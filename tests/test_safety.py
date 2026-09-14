"""
Safety rules the app has to keep, whatever else changes.

- The clinician portal is read-only. A capability token opens one patient's
  record and must never change it. The only writes it may make are its own:
  a row in portal_access_log, and the link's access count and last-accessed time.
- Every page needs a login, except an explicit short list of endpoints that
  authenticate themselves (portal tokens, API bearer tokens) or are the login
  itself. Adding a new public endpoint should mean updating PUBLIC_ENDPOINTS
  here on purpose.
- Token-authenticated API endpoints reject missing, wrong and malformed tokens,
  and fail closed when no token is configured.
"""

import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

PUBLIC_ENDPOINTS = {
    "login", "register", "static", "favicon_files",
    "api_health_sync", "api_flare_status", "api_uv_ingest",
    "portal_view", "portal_section", "portal_document",
}

# What opening a portal page is allowed to change: {table: columns}, None = whole table.
PORTAL_AUDIT_WRITES = {
    "portal_access_log": None,
    "portal_links": {"access_count", "last_accessed_at"},
}


def utc_now():
    """Naive UTC, the form portal links store expires_at in."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def record_contents(db_path, ignore=PORTAL_AUDIT_WRITES):
    """Every row of every table, minus the columns a portal visit may update."""
    con = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        contents = {}
        for table in tables:
            if table in ignore and ignore[table] is None:
                continue
            columns = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
            keep = [c for c in columns if c not in (ignore.get(table) or set())]
            # Sorted in Python: the notes search index keeps tables WITHOUT ROWID,
            # so there is no rowid to order by.
            contents[table] = sorted(
                con.execute(f"SELECT {', '.join(keep)} FROM {table}").fetchall(), key=repr)
        return contents
    finally:
        con.close()


@pytest.fixture
def patient(app_client, fresh_db):
    """A made-up patient with a little of everything the portal shows, a
    second patient, and a valid 30-day portal link to the first."""
    import db
    owner = db.create_user("patient", "Test Patient", "not-a-real-password-hash")
    other = db.create_user("someone", "Someone Else", "not-a-real-password-hash")
    db.upsert_user_preferences(owner, {"patient_name": "Test Patient", "track_cycle": 0})
    today = datetime.now().date()
    for back in range(30):
        db.upsert_daily_observations(owner, {
            "date": (today - timedelta(days=back)).isoformat(),
            "pain_scale": back % 8, "fatigue_scale": 3,
            "migraine": 1 if back % 3 == 0 else 0,
            "flare_occurred": 1 if back == 5 else 0,
        })
    db.add_lab_result(owner, {"date": today.isoformat(), "test_name": "ESR",
                              "numeric_value": 32, "unit": "mm/hr", "flag": "high"})
    db.add_lab_result(owner, {"date": (today - timedelta(days=40)).isoformat(),
                              "test_name": "ESR", "numeric_value": 18, "unit": "mm/hr"})
    db.add_clinical_event(owner, {"date": today.isoformat(), "event_type": "appointment"})
    db.add_medication(owner, {"drug_name": "exampledrug", "dose": 100, "unit": "mg",
                              "start_date": "2025-01-01", "is_primary_intervention": True})
    clinician = db.add_clinician(owner, {"name": "Dr Test", "specialty": "rheumatology"})
    other_doc = db.add_clinical_document(other, {"title": "Someone else's letter",
                                                 "file_name": "other.pdf"})
    link = db.create_portal_link(owner, clinician, "full", "test link",
                                 (utc_now() + timedelta(days=30)).isoformat())
    return {"owner": owner, "token": link["token"], "link_id": link["id"],
            "other_doc": other_doc}


def portal_pages(token):
    from routes.portal import PORTAL_SECTIONS
    return [f"/portal/{token}"] + [f"/portal/{token}/{s}" for s in PORTAL_SECTIONS]


def access_log(db_path):
    con = sqlite3.connect(db_path)
    try:
        return [r[0] for r in con.execute("SELECT path FROM portal_access_log ORDER BY id")]
    finally:
        con.close()


# ------------------------------------------------------------------
# Clinician portal
# ------------------------------------------------------------------

class TestPortalIsReadOnly:
    def test_opening_every_page_changes_nothing_but_its_own_audit_trail(self, app_client, patient, fresh_db):
        before = record_contents(fresh_db)
        for url in portal_pages(patient["token"]):
            assert app_client.get(url).status_code == 200, url
        assert record_contents(fresh_db) == before
        assert access_log(fresh_db) == portal_pages(patient["token"])

    def test_a_link_cannot_open_another_patients_document(self, app_client, patient, fresh_db):
        url = f"/portal/{patient['token']}/document/{patient['other_doc']}"
        assert app_client.get(url).status_code == 404
        assert access_log(fresh_db) == []

    @pytest.mark.parametrize("path", ["", "/labs"])
    def test_an_unknown_token_is_refused(self, app_client, patient, path):
        assert app_client.get(f"/portal/not-a-real-token{path}").status_code == 403

    def test_an_unknown_token_cannot_open_a_document(self, app_client, patient):
        assert app_client.get(f"/portal/not-a-real-token/document/{patient['other_doc']}").status_code == 404

    def test_a_revoked_link_is_refused(self, app_client, patient, fresh_db):
        import db
        db.revoke_portal_link(patient["owner"], patient["link_id"])
        for url in portal_pages(patient["token"]):
            assert app_client.get(url).status_code == 403, url
        assert access_log(fresh_db) == []

    def test_an_expired_link_is_refused(self, app_client, patient, fresh_db):
        import db
        expired = db.create_portal_link(patient["owner"], None, "full", "old link",
                                        (utc_now() - timedelta(minutes=1)).isoformat())
        assert app_client.get(f"/portal/{expired['token']}").status_code == 403
        assert access_log(fresh_db) == []

    def test_a_link_about_to_expire_still_opens(self, app_client, patient):
        # Expiry is compared in UTC. Comparing against local time instead would
        # put every link hours off in one direction or the other; together with
        # the expired-link test above, this catches either mistake.
        import db
        soon = db.create_portal_link(patient["owner"], None, "full", "expiring link",
                                     (utc_now() + timedelta(minutes=1)).isoformat())
        assert app_client.get(f"/portal/{soon['token']}").status_code == 200

    def test_an_unknown_section_is_refused_and_not_logged(self, app_client, patient, fresh_db):
        # Refused before the visit is logged. (This deployment answers 404; the
        # private deployment later switched to a styled 403 page. Either way
        # nothing of the record is served.)
        assert app_client.get(f"/portal/{patient['token']}/admin").status_code == 404
        assert access_log(fresh_db) == []

    def test_portal_pages_are_kept_out_of_indexes_and_caches(self, app_client, patient):
        resp = app_client.get(f"/portal/{patient['token']}")
        assert resp.headers["X-Robots-Tag"] == "noindex, nofollow"
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.headers["Referrer-Policy"] == "no-referrer"


# ------------------------------------------------------------------
# Login gate
# ------------------------------------------------------------------

def sample_url(rule):
    """A concrete URL for a route, with placeholder values for its parameters."""
    samples = {"filename": "favicon.svg", "token": "not-a-real-token", "section": "labs",
               "entry_date": "2026-01-01"}
    return re.sub(r"<(?:(\w+):)?(\w+)>",
                  lambda m: "1" if m.group(1) == "int" else samples.get(m.group(2), "x"),
                  rule.rule)


def test_only_the_expected_endpoints_are_reachable_without_logging_in(app_client, fresh_db):
    import app as app_module
    before = record_contents(fresh_db)
    reachable = set()
    for rule in app_module.app.url_map.iter_rules():
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            resp = app_client.open(sample_url(rule), method=method)
            sent_to_login = (resp.status_code in (301, 302)
                             and resp.headers.get("Location", "").endswith("/login"))
            if not sent_to_login:
                reachable.add(rule.endpoint)
    assert reachable == PUBLIC_ENDPOINTS
    assert record_contents(fresh_db) == before


# ------------------------------------------------------------------
# API bearer tokens
# ------------------------------------------------------------------

SECRET = "correct-horse-battery-staple"

TOKEN_ENDPOINTS = [
    ("POST", "/api/health-sync", "api_token"),
    ("GET", "/api/flare-status?user_id=1", "api_token"),
    ("POST", "/api/uv/ingest", "wearable_token"),
]
ENDPOINT_IDS = [f"{m} {u.split('?')[0]}" for m, u, _ in TOKEN_ENDPOINTS]


@pytest.fixture
def tokens_configured(monkeypatch):
    import appcore
    monkeypatch.setitem(appcore.CONFIG, "api_token", SECRET)
    monkeypatch.setitem(appcore.CONFIG, "wearable_token", SECRET)


class TestAPITokens:
    @pytest.mark.parametrize("method, url, key", TOKEN_ENDPOINTS, ids=ENDPOINT_IDS)
    @pytest.mark.parametrize("header", [
        None,                          # no Authorization header at all
        "Bearer wrong-token",
        SECRET,                        # right token, missing the Bearer scheme
        f"Bearer {SECRET}x",           # right token plus extra characters
        f"Bearer {SECRET[:-1]}",       # a prefix of the right token
        "Bearer caf\u00e9-token",       # non-ASCII: a mismatch, not a server error
    ], ids=["missing", "wrong", "no-scheme", "extra-chars", "prefix", "non-ascii"])
    def test_bad_tokens_are_rejected(self, app_client, tokens_configured, method, url, key, header):
        headers = {"Authorization": header} if header is not None else {}
        assert app_client.open(url, method=method, headers=headers).status_code == 401

    @pytest.mark.parametrize("method, url, key", TOKEN_ENDPOINTS, ids=ENDPOINT_IDS)
    def test_the_right_token_gets_past_the_gate(self, app_client, tokens_configured, monkeypatch,
                                                method, url, key):
        # A control, so the rejections above are not passing vacuously. What
        # happens next (a 400 for an empty body, say) is not this test's concern.
        import app as app_module
        monkeypatch.setitem(app_module.app.config, "PROPAGATE_EXCEPTIONS", False)
        resp = app_client.open(url, method=method, headers={"Authorization": f"Bearer {SECRET}"})
        assert resp.status_code != 401

    @pytest.mark.parametrize("method, url, key", TOKEN_ENDPOINTS, ids=ENDPOINT_IDS)
    def test_an_unconfigured_token_fails_closed(self, app_client, monkeypatch, method, url, key):
        import appcore
        monkeypatch.setitem(appcore.CONFIG, key, "")
        resp = app_client.open(url, method=method, headers={"Authorization": "Bearer "})
        assert resp.status_code == 500

    def test_a_rejected_health_sync_writes_nothing(self, app_client, tokens_configured, patient, fresh_db):
        before = record_contents(fresh_db, ignore={})
        resp = app_client.post("/api/health-sync", headers={"Authorization": "Bearer wrong-token"},
                               json={"user_id": patient["owner"], "date": "2026-01-01", "steps": 12000})
        assert resp.status_code == 401
        assert record_contents(fresh_db, ignore={}) == before


# ------------------------------------------------------------------
# Backup API
# ------------------------------------------------------------------
# The backup routes check a bearer token, but they are not in the login
# allowlist, so on an ordinary server a request without a session is sent to
# /login before the token is looked at. They are reachable without a browser
# session only in single-user mode (the phone-local app), where the sole
# account is signed in automatically and the token check then applies.

BACKUP_ENDPOINTS = [("GET", "/api/backup/export"), ("POST", "/api/backup/restore")]


@pytest.mark.parametrize("method, url", BACKUP_ENDPOINTS, ids=["export", "restore"])
def test_backup_api_needs_a_session_on_an_ordinary_server(app_client, tokens_configured, method, url):
    resp = app_client.open(url, method=method, headers={"Authorization": f"Bearer {SECRET}"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")


class TestBackupAPIInSingleUserMode:
    @pytest.fixture
    def single_user(self, app_client, fresh_db, tokens_configured, monkeypatch):
        import appcore
        import db
        db.create_user("owner", "Owner", "not-a-real-password-hash")
        monkeypatch.setitem(appcore.CONFIG, "single_user_mode", True)
        return app_client

    @pytest.mark.parametrize("method, url", BACKUP_ENDPOINTS, ids=["export", "restore"])
    @pytest.mark.parametrize("header", [None, "Bearer wrong-token", SECRET, f"Bearer {SECRET}x"],
                             ids=["missing", "wrong", "no-scheme", "extra-chars"])
    def test_bad_tokens_are_rejected(self, single_user, method, url, header):
        headers = {"Authorization": header} if header is not None else {}
        assert single_user.open(url, method=method, headers=headers).status_code == 401

    @pytest.mark.parametrize("method, url", BACKUP_ENDPOINTS, ids=["export", "restore"])
    def test_the_right_token_gets_past_the_gate(self, single_user, monkeypatch, method, url):
        import app as app_module
        monkeypatch.setitem(app_module.app.config, "PROPAGATE_EXCEPTIONS", False)
        resp = single_user.open(url, method=method, headers={"Authorization": f"Bearer {SECRET}"})
        assert resp.status_code != 401
