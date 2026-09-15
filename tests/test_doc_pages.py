"""
The in-app document pages tell the truth about the instance they're served from.

- /help renders help.md, the same file the website publishes, so the two can't
  drift apart. It used to be a separate hand-kept copy that had fallen far behind.
- /remote-access used to open with a description of one particular private
  deployment, including "the Oracle VM can't read your data", which was wrong
  there and meaningless everywhere else.
"""

import json
from pathlib import Path

import pytest

import db

REPO = Path(__file__).resolve().parent.parent


def as_embedded_json(text):
    """How text appears once Jinja's tojson has put it inside the page: JSON with
    non-ASCII escaped, plus < > & ' escaped so it can't break out of a <script>."""
    encoded = json.dumps(text)[1:-1]
    for ch, esc in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), ("'", "\\u0027")):
        encoded = encoded.replace(ch, esc)
    return encoded


@pytest.fixture
def signed_in(app_client, fresh_db):
    user_id = db.create_user("patient", "Test Patient", "not-a-real-password-hash")
    with app_client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True
    return app_client


def test_help_shows_help_md(signed_in):
    headings = [line[3:].strip() for line in (REPO / "help.md").read_text().splitlines()
                if line.startswith("## ")]
    assert headings
    resp = signed_in.get("/help")
    assert resp.status_code == 200
    for heading in headings:
        assert as_embedded_json(heading).encode() in resp.data, heading


def test_remote_access_makes_no_claims_about_a_particular_server(signed_in):
    # "Oracle" alone isn't checked: the guide itself rightly mentions Oracle Cloud.
    resp = signed_in.get("/remote-access")
    assert resp.status_code == 200
    for claim in (b"Alaric's house", b"The Oracle VM", b"can't read your data"):
        assert claim not in resp.data, claim
