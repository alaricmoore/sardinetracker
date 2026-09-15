#!/usr/bin/env python3
"""Render the repo's markdown docs into styled pages for sardinetracker.com.

The landing site is an assets-only Cloudflare Worker: anything written into
site/public/ is served at the matching path. So this script reads the markdown
that already lives at the repo root, wraps each file in the site's design
system, and writes site/public/docs/<slug>.html.

The markdown stays the single source of truth. Never hand-edit the generated
HTML -- it is overwritten on every build.

    cd site
    python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
    ./.venv/bin/python build-docs.py
    npx wrangler deploy

SAFETY -- read before adding a document
---------------------------------------
MODEL.md deliberately differs between the private working repo and the public
one, and the private copy contains personal clinical detail that must never be
published. This script therefore refuses to run anywhere except a checkout
whose git origin is the public repo. That guard is the reason it is safe to
add MODEL.md to DOCS later; do not weaken it.
"""

import datetime as dt
import html
import json
import re
import subprocess
import sys
from pathlib import Path

from markdown_it import MarkdownIt
from mdit_py_plugins.anchors import anchors_plugin

SITE = Path(__file__).resolve().parent
ROOT = SITE.parent
OUT = SITE / "public" / "docs"

# Only this repo may publish. See the SAFETY note above.
ALLOWED_ORIGIN = "sardinetracker"
FORBIDDEN_ORIGIN = "private-track"

GITHUB = "https://github.com/alaricmoore/sardinetracker"

# Documents to publish. Adding one is a single entry -- but read the SAFETY
# note first if the document has a private counterpart.
DOCS = [
    {
        "src": "REMOTE_ACCESS.md",
        "slug": "remote-access",
        "nav": "remote access",
        "title": "Remote access",
        "blurb": "Reaching your own instance from outside the house -- a Cloudflare "
                 "Tunnel or a Tailscale VPS, with the trade-offs of each spelled out.",
    },
    {
        "src": "TROUBLESHOOTING.md",
        "slug": "troubleshooting",
        "nav": "troubleshooting",
        "title": "Troubleshooting",
        "blurb": "Symptom-first triage for a deployed instance. What to check, in what "
                 "order, when the site won't load or a device stops syncing.",
    },
    {
        "src": "help.md",
        "slug": "help",
        "nav": "help",
        "title": "Using sardinetracker",
        "blurb": "The in-app help, on the web: what each view is for, what the numbers "
                 "mean, and how to log a day without it becoming a chore.",
    },
    {
        "src": "FEATURES.md",
        "slug": "features",
        "nav": "features",
        "title": "Features",
        "blurb": "Everything the tracker does, one area at a time: daily logging, the "
                 "flare model, interventions, the clinical record and the clinician portal.",
    },
]

# Links to documents we haven't published yet resolve to GitHub instead of 404.
EXTERNAL_MD = {
    "README.md": f"{GITHUB}/blob/main/README.md",
    "MODEL.md": f"{GITHUB}/blob/main/MODEL.md",
    "CONTRIBUTING.md": f"{GITHUB}/blob/main/CONTRIBUTING.md",
    "CHANGELOG.md": f"{GITHUB}/blob/main/CHANGELOG.md",
    "LICENSE": f"{GITHUB}/blob/main/LICENSE",
    "COMMERCIAL_LICENSE.md": f"{GITHUB}/blob/main/COMMERCIAL_LICENSE.md",
}


def guard_repo() -> None:
    """Refuse to build from anywhere but the public checkout."""
    try:
        origin = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit("refusing to build: cannot read the git origin of this checkout")

    if FORBIDDEN_ORIGIN in origin or ALLOWED_ORIGIN not in origin:
        sys.exit(
            f"refusing to build from origin {origin!r}.\n"
            f"This script only runs in the public repo. MODEL.md and others differ\n"
            f"between the private and public trees, and the private copies carry\n"
            f"personal clinical detail. Build from the public checkout instead."
        )


STYLE = """
  :root{
    --bg:#0e0e12; --surface:#16161d; --elevated:#1e1e28;
    --line:#2a2a38; --line2:#38384a;
    --ink:#ffffff; --ink2:rgb(202,196,223); --muted:rgb(159,157,183);
    --accent:#7a8de0; --flare:#e05656;
    --display:'Playfair Display','Times New Roman',Georgia,serif;
    --body:'Source Serif 4','Times New Roman',Georgia,serif;
    --mono:'IBM Plex Mono','Courier New',monospace;
  }
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  html{font-size:16px;scroll-behavior:smooth}
  @media (prefers-reduced-motion: reduce){ html{scroll-behavior:auto} }
  body{background:var(--bg);color:var(--ink);font-family:var(--body);font-weight:300;line-height:1.65}
  a{color:var(--accent);text-decoration:none}
  a:hover{text-decoration:underline}
  a:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:2px}
  .wrap{max-width:920px;margin:0 auto;padding:0 24px}
  code,.mono{font-family:var(--mono);font-size:.875em;color:var(--ink2)}

  header{border-bottom:1px solid var(--line);position:sticky;top:0;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(8px);z-index:10}
  .nav{display:flex;align-items:baseline;justify-content:space-between;gap:16px;padding:14px 0;flex-wrap:wrap}
  .wordmark{font-family:var(--display);font-weight:700;font-size:1.15rem;color:var(--ink);letter-spacing:.02em}
  .wordmark span{color:var(--accent)}
  .nav ul{display:flex;gap:20px;list-style:none;flex-wrap:wrap}
  .nav ul a{font-family:var(--mono);font-size:.8rem;color:var(--ink2)}
  .nav ul a.here{color:var(--accent)}

  .dochead{padding:56px 0 30px;border-bottom:1px solid var(--line)}
  .eyebrow{font-family:var(--mono);font-size:.75rem;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);margin-bottom:16px}
  .dochead h1{font-family:var(--display);font-weight:600;font-size:clamp(1.9rem,5vw,2.9rem);line-height:1.14;letter-spacing:-.01em;max-width:18ch}
  .dochead p{font-size:1.05rem;color:var(--ink2);max-width:60ch;margin-top:18px}

  .layout{display:grid;grid-template-columns:210px 1fr;gap:52px;padding:40px 0 20px;align-items:start}
  @media(max-width:860px){.layout{grid-template-columns:1fr;gap:26px}}

  .toc{position:sticky;top:78px;max-height:calc(100vh - 104px);overflow-y:auto;overscroll-behavior:contain;font-family:var(--mono);font-size:.76rem;line-height:1.5;scrollbar-width:thin;scrollbar-color:var(--line2) transparent}
  .toc::-webkit-scrollbar{width:6px}
  .toc::-webkit-scrollbar-thumb{background:var(--line2);border-radius:3px}
  .toc ul{padding-bottom:8px}
  .toc .lbl{color:var(--muted);letter-spacing:.08em;text-transform:uppercase;font-size:.68rem;display:block;margin-bottom:12px}
  .toc ul{list-style:none;display:flex;flex-direction:column;gap:9px}
  .toc a{color:var(--ink2);display:block}
  .toc .h3{padding-left:12px;border-left:1px solid var(--line);color:var(--muted)}
  @media(max-width:860px){
    .toc{position:static;max-height:172px;overflow-y:auto;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
  }

  .prose{min-width:0;font-size:1.02rem;color:var(--ink2)}
  .prose > *+*{margin-top:20px}
  .prose h2{font-family:var(--display);font-weight:600;font-size:clamp(1.35rem,2.6vw,1.75rem);color:var(--ink);margin-top:52px;line-height:1.25;scroll-margin-top:86px}
  .prose h3{font-family:var(--body);font-weight:600;font-size:1.12rem;color:var(--ink);margin-top:38px;scroll-margin-top:86px}
  .prose h4{font-family:var(--mono);font-weight:400;font-size:.86rem;letter-spacing:.05em;text-transform:uppercase;color:var(--accent);margin-top:32px;scroll-margin-top:86px}
  .prose h2+*,.prose h3+*,.prose h4+*{margin-top:12px}
  .prose p{max-width:68ch}
  .prose strong{color:var(--ink);font-weight:600}
  .prose em{font-style:italic}
  .prose ul,.prose ol{padding-left:22px;max-width:68ch;display:flex;flex-direction:column;gap:8px}
  .prose li{padding-left:4px}
  .prose li::marker{color:var(--muted)}
  .prose blockquote{border-left:3px solid var(--accent);background:var(--surface);border-radius:0 8px 8px 0;padding:14px 18px;font-style:italic;max-width:66ch}
  .prose blockquote p{max-width:none}
  .prose hr{border:0;border-top:1px solid var(--line);margin:44px 0}
  .prose code{background:var(--surface);border:1px solid var(--line);border-radius:4px;padding:1px 5px;color:var(--ink2);white-space:nowrap}
  .prose pre{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:18px 20px;overflow-x:auto;line-height:1.75}
  .prose pre code{background:none;border:0;padding:0;font-size:.82rem;white-space:pre;color:var(--ink2)}
  .prose .tablewrap{overflow-x:auto}
  .prose table{border-collapse:collapse;width:100%;font-size:.92rem;min-width:520px}
  .prose th{text-align:left;font-family:var(--mono);font-weight:400;font-size:.7rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);padding:0 14px 9px 0;border-bottom:1px solid var(--line2);vertical-align:bottom}
  .prose td{padding:12px 14px 12px 0;border-bottom:1px solid var(--line);vertical-align:top}
  .prose td:first-child{color:var(--ink)}
  .prose tr:last-child td{border-bottom:0}
  .prose a code{color:var(--accent)}

  .srconly{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
  .backto{font-family:var(--mono);font-size:.78rem;color:var(--muted);padding:26px 0 0}

  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:12px;overflow:hidden;margin-top:34px}
  .card{background:var(--surface);padding:22px 24px;display:flex;flex-direction:column;gap:8px}
  .card h2{font-family:var(--display);font-weight:600;font-size:1.2rem;color:var(--ink);margin:0}
  .card p{font-size:.93rem;color:var(--ink2);margin:0}
  .card .go{font-family:var(--mono);font-size:.76rem;margin-top:auto;padding-top:8px}

  footer{border-top:1px solid var(--line);padding:36px 0 60px;color:var(--muted);font-size:.85rem;margin-top:56px}
  footer .links{display:flex;gap:20px;flex-wrap:wrap;font-family:var(--mono);font-size:.8rem;margin-bottom:16px}
  footer p{max-width:70ch}
"""

FOOTER = f"""<footer>
  <div class="wrap">
    <div class="links">
      <a href="/">sardinetracker.com</a>
      <a href="{GITHUB}">source &#8599;</a>
      <a href="{GITHUB}/blob/main/MODEL.md">how the model works &#8599;</a>
      <a href="{GITHUB}/blob/main/LICENSE">AGPL-3.0 &#8599;</a>
    </div>
    <p>A one-person project, built between doctor appointments, machine repair, and terrariums.
       Not medical advice; always consult qualified clinicians. Trust your observations.
       Keep asking questions.</p>
  </div>
</footer>"""


def nav(current: str) -> str:
    parts = []
    for d in DOCS:
        cls = ' class="here"' if d["slug"] == current else ""
        parts.append(f'<li><a{cls} href="/docs/{d["slug"]}">{d["nav"]}</a></li>')
    items = "".join(parts)
    return f"""<header>
  <div class="wrap nav">
    <div class="wordmark"><a href="/" style="color:inherit">sardine<span>tracker</span></a></div>
    <ul>{items}<li><a href="{GITHUB}">github &#8599;</a></li></ul>
  </div>
</header>"""


def shell(*, title, desc, canonical, body, current="") -> str:
    # TechArticle so a crawler -- or a model answering a question -- can tell
    # what this page is and who wrote it without parsing the prose.
    schema = json.dumps({
        "@context": "https://schema.org",
        "@type": "TechArticle",
        "headline": title,
        "description": desc,
        "url": canonical,
        "inLanguage": "en",
        "isPartOf": {"@type": "WebSite", "@id": "https://sardinetracker.com/#site"},
        "about": {"@type": "SoftwareApplication", "@id": "https://sardinetracker.com/#app"},
        "author": {"@type": "Person", "name": "Alaric Moore"},
        "license": "https://www.gnu.org/licenses/agpl-3.0.html",
    }, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} &#8212; sardinetracker</title>
<meta name="description" content="{html.escape(desc)}">
<link rel="canonical" href="{canonical}">
<link rel="icon" href="/favicon.ico" sizes="48x48">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta property="og:type" content="article">
<meta property="og:url" content="{canonical}">
<meta property="og:site_name" content="sardinetracker">
<meta property="og:title" content="{html.escape(title)} &#8212; sardinetracker">
<meta property="og:description" content="{html.escape(desc)}">
<meta property="og:image" content="https://sardinetracker.com/og-image.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;600;700&amp;family=Source+Serif+4:ital,opsz,wght@0,8..60,300;0,8..60,400;0,8..60,600;1,8..60,300;1,8..60,400&amp;family=IBM+Plex+Mono:wght@300;400&amp;display=swap" rel="stylesheet">
<style>{STYLE}</style>
<script type="application/ld+json">{schema}</script>
</head>
<body>
{nav(current)}
{body}
{FOOTER}
</body>
</html>
"""


def rewrite_links(body: str) -> str:
    """Point .md links at their published page, or at GitHub if unpublished."""
    local = {d["src"]: f'/docs/{d["slug"]}' for d in DOCS}

    def sub(m):
        target, anchor = m.group(1), m.group(2) or ""
        if target in local:
            return f'href="{local[target]}{anchor}"'
        if target in EXTERNAL_MD:
            return f'href="{EXTERNAL_MD[target]}{anchor}"'
        return f'href="{GITHUB}/blob/main/{target}{anchor}"'

    body = re.sub(r'href="([A-Z_]+\.md|[a-z_]+\.md|LICENSE)(#[^"]*)?"', sub, body)

    # A rendered page shouldn't show raw filenames as link text. Where a link's
    # visible text is just the source filename, swap in the published title.
    for d in DOCS:
        body = re.sub(
            rf'(<a href="/docs/{d["slug"]}"[^>]*>){re.escape(d["src"])}(</a>)',
            rf'\g<1>{d["title"].lower()}\g<2>',
            body,
        )
    return body


def make_parser() -> MarkdownIt:
    """CommonMark, not python-markdown.

    python-markdown's fenced_code only recognises fences at column 0, so every
    code block nested inside a numbered step silently degraded to inline code
    and restarted the list. The docs are valid CommonMark; the parser has to be
    too.
    """
    return (
        MarkdownIt("commonmark", {"html": False})
        .enable(["table", "strikethrough"])
        .use(anchors_plugin, max_level=4)
    )


def render(text: str):
    """Return (html, [(tag, anchor, title), ...]) for the headings."""
    md = make_parser()
    tokens = md.parse(text, {})
    toc = []
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open" and tok.tag in ("h2", "h3"):
            toc.append((tok.tag, tok.attrGet("id"), tokens[i + 1].content))
    return md.render(text), toc


def build_toc(entries) -> str:
    rows = []
    for tag, anchor, title in entries:
        cls = ' class="h3"' if tag == "h3" else ""
        rows.append(f'<li{cls}><a href="#{anchor}">{html.escape(title)}</a></li>')
    return (
        '<nav class="toc" aria-label="On this page">'
        '<span class="lbl">On this page</span>'
        f'<ul>{"".join(rows)}</ul></nav>'
    )


def build_doc(doc) -> None:
    src = ROOT / doc["src"]
    text = src.read_text(encoding="utf-8")

    # The leading "# Title" becomes the page header, not body content.
    text = re.sub(r"\A#\s+.*?\n", "", text, count=1)

    raw, toc = render(text)
    body = rewrite_links(raw)
    body = body.replace("<table>", '<div class="tablewrap"><table>').replace(
        "</table>", "</table></div>"
    )

    canonical = f'https://sardinetracker.com/docs/{doc["slug"]}'
    page = shell(
        title=doc["title"],
        desc=doc["blurb"].replace("--", "—"),
        canonical=canonical,
        current=doc["slug"],
        body=f"""<main>
  <div class="wrap dochead">
    <p class="eyebrow">documentation</p>
    <h1>{html.escape(doc["title"])}</h1>
    <p>{html.escape(doc["blurb"]).replace("--", "&#8212;")}</p>
  </div>
  <div class="wrap layout">
    {build_toc(toc)}
    <article class="prose">{body}</article>
  </div>
  <div class="wrap backto"><a href="/docs">&#8592; all documentation</a></div>
</main>""",
    )
    (OUT / f'{doc["slug"]}.html').write_text(page, encoding="utf-8")
    print(f'  {doc["src"]:<22} -> public/docs/{doc["slug"]}.html  ({len(page)//1024} KB)')


def build_index() -> None:
    cards = "".join(
        f"""<div class="card">
      <h2>{html.escape(d["title"])}</h2>
      <p>{html.escape(d["blurb"]).replace("--", "&#8212;")}</p>
      <p class="go"><a href="/docs/{d["slug"]}">read &#8594;</a></p>
    </div>"""
        for d in DOCS
    )
    page = shell(
        title="Documentation",
        desc="Guides for running your own sardinetracker instance: remote access, "
             "troubleshooting, and how to use the app day to day.",
        canonical="https://sardinetracker.com/docs",
        body=f"""<main>
  <div class="wrap dochead">
    <p class="eyebrow">documentation</p>
    <h1>Running your own instance.</h1>
    <p>sardinetracker is meant to live on your machine, which means the parts nobody
       else can do for you &#8212; getting to it from outside the house, and working out
       what broke when it stops answering &#8212; are documented here rather than assumed.</p>
  </div>
  <div class="wrap"><div class="cards">{cards}</div></div>
</main>""",
    )
    (OUT / "index.html").write_text(page, encoding="utf-8")
    print(f"  {'(index)':<22} -> public/docs/index.html  ({len(page)//1024} KB)")


SITE_URL = "https://sardinetracker.com"


def build_sitemap() -> None:
    """Every published URL, with a real lastmod from its source file."""
    pages = [("/", ROOT / "site" / "public" / "index.html", "1.0"),
             ("/docs", None, "0.8")]
    pages += [(f'/docs/{d["slug"]}', ROOT / d["src"], "0.7") for d in DOCS]

    rows = []
    for path, src, priority in pages:
        when = (dt.date.fromtimestamp(src.stat().st_mtime).isoformat()
                if src and src.exists() else dt.date.today().isoformat())
        rows.append(
            f"  <url>\n    <loc>{SITE_URL}{path}</loc>\n"
            f"    <lastmod>{when}</lastmod>\n"
            f"    <priority>{priority}</priority>\n  </url>"
        )
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           + "\n".join(rows) + "\n</urlset>\n")
    (SITE / "public" / "sitemap.xml").write_text(xml, encoding="utf-8")
    print(f"  {'(sitemap)':<22} -> public/sitemap.xml  ({len(pages)} urls)")


def build_robots() -> None:
    """Our own robots.txt.

    Cloudflare injects a managed one that disallows every AI crawler by
    default -- ClaudeBot, GPTBot and CCBot among them. CCBot is Common Crawl,
    so that default is what keeps this site out of model training data. This
    file states the opposite intent; the managed robots.txt has to be turned
    off in the dashboard for it to be served.
    """
    txt = f"""# sardinetracker.com
# Open documentation for a self-hosted health tracker. Indexing, retrieval and
# training are all welcome -- the point is for people looking for this to find
# it, including through whatever they happen to ask.

User-agent: *
Content-Signal: search=yes,ai-train=yes,use=reference
Allow: /

Sitemap: {SITE_URL}/sitemap.xml
"""
    (SITE / "public" / "robots.txt").write_text(txt, encoding="utf-8")
    print(f"  {'(robots)':<22} -> public/robots.txt")


def main() -> None:
    guard_repo()
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"building {len(DOCS)} docs into {OUT.relative_to(ROOT)}/")
    for doc in DOCS:
        build_doc(doc)
    build_index()
    build_sitemap()
    build_robots()
    print("done. deploy with: npx wrangler deploy")


if __name__ == "__main__":
    main()
