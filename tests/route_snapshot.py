#!/usr/bin/env python3
"""
Behaviour snapshot for app.py: record what every page returns, then prove a
refactor changed nothing.

    record   import the app against a throwaway COPY of a database, log in as
             user 1, GET every route that can be reached without side effects
             on real data, and write what came back
    compare  diff two recordings

    python tests/route_snapshot.py record --db biotracking.db --out /tmp/snap-before
    python tests/route_snapshot.py record --db biotracking.db --out /tmp/snap-after
    python tests/route_snapshot.py compare /tmp/snap-before /tmp/snap-after

What gets compared
    routes     every URL rule, its endpoint name and its methods. Catches a
               route that was dropped or renamed in the move - including the
               POST routes, which are never called.
    source     a hash of every function and class body defined in this repo.
               Code moved byte-for-byte hashes identically wherever it lands,
               so this covers the code paths no GET reaches.
    responses  status, content type, redirect target, and a hash of the body
               for every GET.

Safety - a snapshot runs the real app, so all of these are enforced:
    * the database is COPIED into a temp directory; the original is never
      opened. The app runs with that directory as its cwd and as
      SARDINE_DATA_DIR, and the script refuses to continue if db.py would
      open anything else. The folder gets its own config.json: the
      checkout's if it has one, otherwise config.json.example with a fixed
      secret key.
    * all outbound network connections are refused (no ntfy pushes, no UV API)
    * the background scheduler is never started
    * --out must be outside the repo: rendered pages contain health data

Dates: pages compute "today", so compare two recordings made on the same day.
"""

import argparse, hashlib, inspect, io, json, os, re, shutil, socket, sqlite3, sys, tempfile, time, zipfile
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_WORK = None   # the throwaway data folder for the current recording, set by record()
PORTAL_TOKEN = "snapshot-portal-token"

# Masked before hashing so two runs of unchanged code match. Anything that
# still differs between two recordings of unchanged code is noise the harness
# does not know about yet - run `record` twice before trusting a comparison.
_CSRF = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}")


# ------------------------------------------------------------
# isolation
# ------------------------------------------------------------

def _refuse_network():
    def blocked(*a, **k):
        raise ConnectionRefusedError("route_snapshot: network disabled")
    socket.socket.connect = blocked
    socket.create_connection = blocked


def _refuse_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    BackgroundScheduler.start = lambda self, *a, **k: None


def _prepare_config(work: Path) -> None:
    """config.json for the data folder. A checkout without one (a fresh clone
    of the public repo) gets the example, with a fixed secret key so session
    and CSRF signing is the same on every run."""
    target = work / "config.json"
    if (REPO / "config.json").exists():
        shutil.copy(REPO / "config.json", target)
        return
    example = json.loads((REPO / "config.json.example").read_text())
    example["secret_key"] = "route-snapshot-fixed-secret"
    target.write_text(json.dumps(example))


def _prepare_db(src: Path, work: Path) -> Path:
    dst = work / "biotracking.db"
    # sqlite's backup API gives a consistent copy even if the source is in use
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(dst) as d:
        s.backup(d)
    with sqlite3.connect(dst) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(portal_links)")]
        row = c.execute("SELECT * FROM portal_links ORDER BY id LIMIT 1").fetchone()
        if row:
            vals = dict(zip(cols, row))
            vals.pop("id")
            vals["token"] = PORTAL_TOKEN
            for k in ("expires_at", "revoked_at"):
                if k in vals:
                    vals[k] = None
            c.execute(f"INSERT INTO portal_links ({', '.join(vals)}) VALUES "
                      f"({', '.join('?' * len(vals))})", list(vals.values()))
    return dst


# ------------------------------------------------------------
# fingerprints
# ------------------------------------------------------------

def _sha(b) -> str:
    return hashlib.sha256(b if isinstance(b, bytes) else b.encode()).hexdigest()[:16]


def _source_fingerprints() -> list:
    out = set()
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None) or ""
        if not f.startswith(str(REPO)) or "/tests/" in f:
            continue
        for attr, obj in vars(mod).items():
            if (inspect.isfunction(obj) or inspect.isclass(obj)) and obj.__module__ == name:
                try:
                    out.add((obj.__qualname__, _sha(inspect.getsource(obj))))
                except (OSError, TypeError):
                    pass
    return sorted(out)


_SIMPLE = (str, int, float, bool, type(None), tuple, list, dict, set, frozenset)


def _canon(obj) -> str:
    """repr, except sets are written sorted.

    A set's printed order depends on how it was built, and a set literal is
    built from a constant stored in the .pyc - in whatever order the compiling
    process had it. So identical code prints the same set differently depending
    on which __pycache__ it loaded. Sets have no order, so compare them sorted.
    """
    if isinstance(obj, (set, frozenset)):
        return "{" + ", ".join(sorted(_canon(x) for x in obj)) + "}"
    if isinstance(obj, dict):
        return "{" + ", ".join(f"{_canon(k)}: {_canon(v)}" for k, v in obj.items()) + "}"
    if isinstance(obj, (list, tuple)):
        inner = ", ".join(_canon(x) for x in obj)
        return f"[{inner}]" if isinstance(obj, list) else f"({inner})"
    return repr(obj)


def _constant_fingerprints() -> list:
    """Module-level plain values (paths, lookup tables, thresholds) in this repo.

    A path constant that resolves differently after a move - DOCUMENTS_DIR
    pointing into a new subfolder, say - changes no page when the database
    holds no documents, so only its value can catch it.
    """
    out = set()
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None) or ""
        if not f.startswith(str(REPO)) or "/tests/" in f:
            continue
        for attr, obj in vars(mod).items():
            if not attr.startswith("__") and isinstance(obj, _SIMPLE):
                # Paths are compared relative to the checkout, so a copy of the
                # repo elsewhere records the same values.
                text = _canon(obj).replace(str(REPO), "<REPO>")
                if _WORK:
                    text = text.replace(str(_WORK), "<DATA>")
                # A table holding lambdas prints their memory addresses, which
                # change every run. The lambdas' code is covered by source hashing.
                if " at 0x" not in text:
                    out.add((attr, _sha(text)))
    return sorted(out)


def _route_table(app) -> list:
    return sorted((r.rule, r.endpoint, sorted(r.methods - {"HEAD", "OPTIONS"}))
                  for r in app.url_map.iter_rules())


def _body_digest(resp) -> tuple:
    data = resp.get_data()
    ctype = resp.headers.get("Content-Type", "")
    if data[:2] == b"PK":     # zip archives embed their creation time
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            listing = "\n".join(f"{i.filename} {i.CRC}" for i in sorted(z.infolist(), key=lambda i: i.filename))
        return _sha(listing), listing
    if ctype.startswith(("text/", "application/json", "application/javascript")):
        text = _CSRF.sub("<CSRF>", data.decode("utf-8", "replace"))
        if _WORK:
            text = text.replace(str(_WORK), "<DATA>")
        return _sha(text), text
    return _sha(data), None


# ------------------------------------------------------------
# record
# ------------------------------------------------------------

def _get_urls(app, conn) -> list:
    last_date = (conn.execute("SELECT max(date) FROM daily_observations WHERE user_id = 1").fetchone() or [None])[0]
    samples = {"entry_date": last_date or date.today().isoformat(), "token": PORTAL_TOKEN,
               "filename": "favicon.svg", "doc_id": 1}
    urls, skipped = [], []
    for r in app.url_map.iter_rules():
        if "GET" not in r.methods or r.endpoint == "static":
            continue
        if any(a not in samples for a in r.arguments):
            skipped.append(r.rule)
            continue
        url = r.rule
        for a in r.arguments:
            url = re.sub(rf"<(?:[a-z]+:)?{a}>", str(samples[a]), url)
        urls.append((url, r.endpoint))
    # Anything that could end the session or remove data goes last. Token
    # portal pages log a timestamped view that /portals then displays, so they
    # go after it - otherwise two runs a minute apart differ.
    urls.sort(key=lambda u: (any(w in u[1] for w in ("logout", "delete", "reset", "revoke")),
                             u[1] in ("portal_view", "portal_section", "portal_document"), u[0]))
    return urls, skipped


def record(db_src: Path, out: Path):
    if REPO in out.resolve().parents or out.resolve() == REPO:
        sys.exit("refusing: --out is inside the repo, and rendered pages contain health data")
    if out.exists() and any(out.iterdir()):
        sys.exit(f"refusing: {out} is not empty")
    (out / "bodies").mkdir(parents=True, exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix="route-snapshot-"))
    db_copy = _prepare_db(db_src.resolve(), work)
    global _WORK
    _WORK = work
    _prepare_config(work)
    os.environ["SARDINE_DATA_DIR"] = str(work)
    _refuse_network()
    _refuse_scheduler()
    os.chdir(work)
    sys.path.insert(0, str(REPO))

    log = open(out / "app-output.log", "w")
    real_stdout, sys.stdout = sys.stdout, log
    try:
        import app as app_module
        import db
    finally:
        sys.stdout = real_stdout
    if Path(db.DB_FILE).resolve() != db_copy:
        sys.exit(f"refusing: app would use {Path(db.DB_FILE).resolve()}, not the copy")

    app = app_module.app
    app.config.update(TESTING=True)

    portal_sections = set()
    results = {}
    with sqlite3.connect(db_copy) as conn:
        urls, skipped = _get_urls(app, conn)

    def fetch(url, endpoint):
        client = app.test_client()
        with client.session_transaction() as s:
            s["_user_id"] = "1"
            s["_fresh"] = True
        t0 = time.monotonic()
        sys.stdout = log
        try:
            resp = client.get(url)
            digest, text = _body_digest(resp)
            entry = {"endpoint": endpoint, "status": resp.status_code,
                     "type": resp.headers.get("Content-Type", ""),
                     "location": resp.headers.get("Location"), "body": digest}
        except Exception as e:
            text = None
            entry = {"endpoint": endpoint, "status": "EXCEPTION", "body": _sha(repr(e)), "error": repr(e)}
        finally:
            sys.stdout = real_stdout
        entry["seconds"] = round(time.monotonic() - t0, 2)
        if text is not None:
            (out / "bodies" / (re.sub(r"[^A-Za-z0-9]+", "_", url).strip("_") or "root")).with_suffix(".txt").write_text(text)
        results[url] = entry
        print(f"  {entry['status']!s:>9}  {entry['seconds']:6.2f}s  {url}", flush=True)
        return text

    for url, endpoint in urls:
        text = fetch(url, endpoint)
        if endpoint == "portal_view" and text:
            portal_sections |= set(re.findall(rf"/portal/{PORTAL_TOKEN}/([a-z0-9_-]+)\b", text))
    for section in sorted(portal_sections - {"document"}):
        fetch(f"/portal/{PORTAL_TOKEN}/{section}", "portal_section")

    manifest = {"recorded_on": date.today().isoformat(), "db_source": str(db_src),
                "routes": _route_table(app), "source": _source_fingerprints(),
                "constants": _constant_fingerprints(),
                "responses": results, "skipped_rules": sorted(skipped)}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    shutil.rmtree(work, ignore_errors=True)
    bad = [u for u, e in results.items() if e["status"] == "EXCEPTION"]
    print(f"\n{len(results)} GETs recorded, {len(manifest['routes'])} rules, "
          f"{len(manifest['source'])} functions/classes, {len(skipped)} rules skipped"
          + (f", {len(bad)} raised exceptions" if bad else ""))


# ------------------------------------------------------------
# compare
# ------------------------------------------------------------

def compare(a: Path, b: Path) -> int:
    A = json.loads((a / "manifest.json").read_text())
    B = json.loads((b / "manifest.json").read_text())
    problems = 0

    if A["recorded_on"] != B["recorded_on"]:
        print(f"WARNING: recorded on different days ({A['recorded_on']} vs {B['recorded_on']}); "
              "date-dependent pages will differ for that reason alone\n")

    ra = {tuple(map(str, r)) for r in A["routes"]}
    rb = {tuple(map(str, r)) for r in B["routes"]}
    for r in sorted(ra - rb):
        print(f"ROUTE MISSING   {r}"); problems += 1
    for r in sorted(rb - ra):
        print(f"ROUTE ADDED     {r}"); problems += 1

    sa, sb = {tuple(x) for x in A["source"]}, {tuple(x) for x in B["source"]}
    names_a, names_b = {n for n, _ in sa}, {n for n, _ in sb}
    for n in sorted(names_a - names_b):
        print(f"CODE MISSING    {n}"); problems += 1
    for n in sorted(names_b - names_a):
        print(f"CODE ADDED      {n}"); problems += 1
    for n in sorted(names_a & names_b):
        if {h for m, h in sa if m == n} != {h for m, h in sb if m == n}:
            print(f"CODE CHANGED    {n}"); problems += 1

    ca, cb = {tuple(x) for x in A.get("constants", [])}, {tuple(x) for x in B.get("constants", [])}
    vals_a, vals_b = {n for n, _ in ca}, {n for n, _ in cb}
    for n in sorted(vals_a - vals_b):
        print(f"VALUE MISSING   {n}"); problems += 1
    for n in sorted(vals_b - vals_a):
        print(f"VALUE ADDED     {n}"); problems += 1
    for n in sorted(vals_a & vals_b):
        if {h for m, h in ca if m == n} != {h for m, h in cb if m == n}:
            print(f"VALUE CHANGED   {n}"); problems += 1

    for url in sorted(set(A["responses"]) | set(B["responses"])):
        ea, eb = A["responses"].get(url), B["responses"].get(url)
        if ea is None or eb is None:
            print(f"GET {'MISSING' if eb is None else 'ADDED  '}     {url}"); problems += 1
            continue
        diffs = [k for k in ("status", "type", "location", "body") if ea.get(k) != eb.get(k)]
        if diffs:
            print(f"GET DIFFERS     {url}  ({', '.join(diffs)})"); problems += 1

    print(f"\n{'IDENTICAL' if not problems else f'{problems} difference(s)'}: "
          f"{len(rb)} routes, {len(sb)} functions/classes, {len(cb)} values, {len(B['responses'])} GETs")
    return 1 if problems else 0


if __name__ == "__main__":
    # String hashing is randomised per process, which reorders anything a page
    # renders out of a set. Pin it so two runs of the same code agree.
    if os.environ.get("PYTHONHASHSEED") != "0":
        os.environ["PYTHONHASHSEED"] = "0"
        os.execv(sys.executable, [sys.executable] + sys.argv)

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--db", type=Path, default=REPO / "biotracking.db")
    r.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("compare")
    c.add_argument("a", type=Path)
    c.add_argument("b", type=Path)
    args = ap.parse_args()
    if args.cmd == "record":
        record(args.db, args.out)
    else:
        sys.exit(compare(args.a, args.b))
