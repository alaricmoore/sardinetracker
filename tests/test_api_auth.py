"""
Signed API requests: the rules in api_signing.py and apiauth.py.

- The wire format is pinned by known-answer vectors, so a port to Swift,
  Kotlin or C can prove itself against these numbers before it talks to a server.
- A good signature gets in. A wrong secret, a changed method, path, query or
  body, a stale or future timestamp, a reused nonce, an unknown client, and
  malformed headers are all the same 401, and write nothing.
- A device with no clock is accepted only while its (boot id, uptime) counter
  goes up.
- A signed client without the endpoint's permit, or acting for a user it isn't
  bound to, gets 403.
- The backup routes need the backup permit and still sit behind the login gate,
  so without a session they are reachable only in single_user_mode.
- Legacy bearer tokens open only what they opened before permits, and nothing
  once allow_bearer is off.
"""

import hashlib
import hmac
import sqlite3
import time

import pytest

import api_signing

SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
OTHER_SECRET = "f" * 64
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


# ------------------------------------------------------------------
# Wire format
# ------------------------------------------------------------------

class TestWireFormat:
    @pytest.mark.parametrize("raw, canonical", [
        ("", ""),
        ("user_id=1", "user_id=1"),
        ("b=2&a=1", "a=1&b=2"),                # order doesn't matter
        ("a=%31&b=2", "a=1&b=2"),              # nor needless escaping
        ("a=x+y", "a=x%20y"),                  # + is a space in a query
        ("a=x%20y", "a=x%20y"),
        ("a=%7e", "a=~"),                      # ~ is left bare
        ("a=2&a=1", "a=1&a=2"),                # repeated keys sort by value
        ("&&c=&a=1&", "a=1&c="),               # empty parts dropped, empty values kept
        ("k/=v&", "k%2F=v"),
    ])
    def test_canonical_query(self, raw, canonical):
        assert api_signing.canonical_query(raw) == canonical

    def test_the_string_to_sign_is_eight_lines(self):
        # Written out by hand, not built by the code under test.
        assert api_signing.string_to_sign(
            "get", "/api/clinicians", "user_id=1&a=%7e", "qwen", "1789430400",
            "abcdefghijklmnop", b"",
        ) == ("TR1\nGET\n/api/clinicians\na=~&user_id=1\nqwen\n1789430400\n"
              "abcdefghijklmnop\n" + EMPTY_SHA256)

    # Known answers. The secret is SECRET, read as UTF-8 text (not hex-decoded).
    # The client names and paths are arbitrary example strings, not endpoints
    # this app must have: they are kept identical to the other signers' test
    # vectors (the iOS app's included) so every port checks against the same numbers.
    VECTORS = [
        ("GET", "/api/clinicians", "user_id=1&a=%7e", "qwen", "1789430400",
         "abcdefghijklmnop", b"",
         "91f1e65c5db6e943755aeeaa295f14aae0a1af044cbec99fa164eed618b8e499"),
        ("POST", "/api/health-sync", "", "qwen", "1789430400",
         "abcdefghijklmnop", b'{"user_id":1,"steps":12000}',
         "dbb273dbfabf7dff5ede7f2358fd070627f5e0e4ebc8c4c87476e84b31527701"),
        ("POST", "/api/uv/ingest", "", "wearable", "42.123456",
         "", b"42,123000,10,20,1,2,3900\n",
         "8df17852a10f8a062b1b7ebb67e410d1304d4ab512428ceab6481f545fc356a5"),
    ]

    @pytest.mark.parametrize("method, path, query, client, stamp, nonce, body, expected", VECTORS,
                             ids=["get-with-query", "post-json", "counter-client"])
    def test_known_answers(self, method, path, query, client, stamp, nonce, body, expected):
        assert api_signing.sign(SECRET, method, path, query, client, stamp, nonce, body) == expected
        text = api_signing.string_to_sign(method, path, query, client, stamp, nonce, body)
        assert hmac.new(SECRET.encode(), text.encode(), hashlib.sha256).hexdigest() == expected


# ------------------------------------------------------------------
# Fixtures and helpers
# ------------------------------------------------------------------

def signed_clients(owner):
    return {
        "phone": {"secret": SECRET, "permits": ["health_sync", "flare_status"], "user_ids": [owner]},
        "watcher": {"secret": SECRET, "permits": ["flare_status"], "user_ids": [owner]},
        "wearable": {"secret": SECRET, "permits": ["uv_ingest"], "user_ids": [owner], "clock": "counter"},
        "unbound": {"secret": SECRET, "permits": ["flare_status"]},
        "backups": {"secret": SECRET, "permits": ["backup"], "user_ids": [owner]},
    }


@pytest.fixture
def users(fresh_db):
    import db
    owner = db.create_user("patient", "Test Patient", "not-a-real-password-hash")
    other = db.create_user("someone", "Someone Else", "not-a-real-password-hash")
    return {"owner": owner, "other": other}


@pytest.fixture
def clients(app_client, users, monkeypatch):
    """Signed clients only: the legacy bearer tokens are switched off."""
    import appcore
    owner = users["owner"]
    monkeypatch.setitem(appcore.CONFIG, "api_token", "")
    monkeypatch.setitem(appcore.CONFIG, "wearable_token", "")
    monkeypatch.setitem(appcore.CONFIG, "wearable_user_id", owner)
    monkeypatch.setitem(appcore.CONFIG, "api_clients", signed_clients(owner))
    return users


def send(app_client, method, url, client="phone", secret=SECRET, body=b"",
         content_type=None, sign_url=None, send_url=None, send_body=None, headers=None,
         **sign_kwargs):
    """Sign a request for `url` and `body`, then send it, optionally altered on the way."""
    signed = api_signing.signed_headers(client, secret, method, sign_url or url, body, **sign_kwargs)
    signed.update(headers or {})
    signed = {k: v for k, v in signed.items() if v is not None}
    return app_client.open(send_url or url, method=method, headers=signed,
                           data=body if send_body is None else send_body,
                           content_type=content_type)


def table_count(db_path, table):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def steps_on(user_id, day):
    import db
    return (db.get_daily_observations(user_id, day) or {}).get("steps")


def health_sync_body(user_id, steps=12000):
    return f'{{"user_id": {user_id}, "date": "2026-01-01", "steps": {steps}}}'.encode()


# ------------------------------------------------------------------
# Clients with a clock
# ------------------------------------------------------------------

class TestSignedRequests:
    def test_a_signed_get_gets_in(self, app_client, clients):
        resp = send(app_client, "GET", f"/api/flare-status?user_id={clients['owner']}")
        assert resp.status_code == 200
        assert "ok" in resp.get_json()

    def test_a_signed_post_writes(self, app_client, clients):
        resp = send(app_client, "POST", "/api/health-sync",
                    body=health_sync_body(clients["owner"]), content_type="application/json")
        assert resp.status_code == 200, resp.get_json()
        assert steps_on(clients["owner"], "2026-01-01") == 12000

    def test_a_timestamp_inside_the_window_is_accepted(self, app_client, clients):
        resp = send(app_client, "GET", f"/api/flare-status?user_id={clients['owner']}",
                    now=int(time.time()) - api_signing.WINDOW_SECONDS + 10)
        assert resp.status_code == 200

    @pytest.mark.parametrize("change", [
        dict(secret=OTHER_SECRET),
        dict(send_body=health_sync_body(1, steps=99999)),
        dict(send_url="/api/health-sync?user_id=1"),
        dict(sign_url="/api/flare-status"),            # phone may call both; the path is still signed
        dict(headers={"X-Client-Id": "watcher"}),        # signed as phone, claims to be watcher
        dict(client="nobody"),
        dict(now=int(time.time()) - api_signing.WINDOW_SECONDS - 5),
        dict(now=int(time.time()) + api_signing.WINDOW_SECONDS + 5),
        dict(headers={"X-Signature": None}),
        dict(headers={"X-Timestamp": None}),
        dict(headers={"X-Nonce": None}),
        dict(headers={"X-Signature": "A" * 64}),         # upper-case hex is not the format
        dict(headers={"X-Signature": "zz"}),
        dict(headers={"X-Timestamp": "1789430400.5"}),
        dict(nonce="short"),
        dict(nonce="has/a/slash/in/it/ok"),
        dict(headers={"X-Nonce": "café-café-café-café"}),  # non-ASCII: refused, not a server error
    ], ids=["wrong-secret", "altered-body", "added-query", "altered-path", "other-client-id",
            "unknown-client", "stale", "future", "no-signature", "no-timestamp", "no-nonce",
            "uppercase-signature", "short-signature", "fractional-timestamp", "short-nonce",
            "nonce-bad-chars", "non-ascii-nonce"])
    def test_bad_requests_are_refused_and_write_nothing(self, app_client, clients, fresh_db, change):
        import appcore
        user = clients["owner"]
        if change.get("send_body"):
            change["send_body"] = health_sync_body(user, steps=99999)
        resp = send(app_client, "POST", "/api/health-sync", **{
            "body": health_sync_body(user), "content_type": "application/json", **change})
        assert resp.status_code == 401
        assert resp.get_json() == {"error": "unauthorized"}
        assert steps_on(user, "2026-01-01") is None
        assert table_count(fresh_db, "api_nonces") == 0
        assert "phone" in appcore.CONFIG["api_clients"]  # the fixture really was in place

    def test_a_replayed_request_is_refused(self, app_client, clients):
        url = f"/api/flare-status?user_id={clients['owner']}"
        headers = api_signing.signed_headers("phone", SECRET, "GET", url)
        assert app_client.get(url, headers=headers).status_code == 200
        assert app_client.get(url, headers=headers).status_code == 401

    def test_nonces_are_forgotten_once_the_window_has_passed(self, fresh_db):
        import db
        assert db.remember_api_nonce("c", "n" * 16, now=1000, window=300) is True
        assert db.remember_api_nonce("c", "n" * 16, now=1300, window=300) is False
        assert db.remember_api_nonce("other", "n" * 16, now=1300, window=300) is True
        assert db.remember_api_nonce("c", "n" * 16, now=1301, window=300) is True

    def test_an_oversized_body_is_refused_before_it_is_read(self, app_client, clients):
        body = b"x" * (1024 * 1024 + 1)
        resp = send(app_client, "POST", "/api/health-sync", body=body, content_type="application/json")
        assert resp.status_code == 413

    def test_an_unknown_permit_is_a_programming_error(self):
        from apiauth import require_client
        with pytest.raises(ValueError):
            require_client("delete_everything")


# ------------------------------------------------------------------
# Permits and user binding
# ------------------------------------------------------------------

class TestPermits:
    def test_a_client_without_the_permit_is_forbidden(self, app_client, clients):
        user = clients["owner"]
        resp = send(app_client, "POST", "/api/health-sync", client="watcher",
                    body=health_sync_body(user), content_type="application/json")
        assert resp.status_code == 403
        assert steps_on(user, "2026-01-01") is None

    @pytest.mark.parametrize("client", ["phone", "watcher"])
    def test_a_client_cannot_act_for_a_user_it_is_not_bound_to(self, app_client, clients, client):
        resp = send(app_client, "GET", f"/api/flare-status?user_id={clients['other']}", client=client)
        assert resp.status_code == 403

    def test_a_user_that_does_not_exist_is_forbidden_not_not_found(self, app_client, clients):
        # Otherwise a bound client could probe which user ids exist.
        resp = send(app_client, "POST", "/api/health-sync",
                    body=health_sync_body(9999), content_type="application/json")
        assert resp.status_code == 403

    def test_a_client_with_no_user_list_acts_for_nobody(self, app_client, clients):
        resp = send(app_client, "GET", f"/api/flare-status?user_id={clients['owner']}", client="unbound")
        assert resp.status_code == 403

    def test_a_403_says_why(self, app_client, clients):
        no_permit = send(app_client, "POST", "/api/health-sync", client="watcher",
                         body=health_sync_body(clients["owner"]), content_type="application/json")
        assert no_permit.status_code == 403
        assert no_permit.get_json() == {"error": "forbidden",
                                        "reason": "client 'watcher' has no health_sync permit"}
        unbound = send(app_client, "GET", f"/api/flare-status?user_id={clients['owner']}", client="unbound")
        assert unbound.status_code == 403
        assert unbound.get_json()["error"] == "forbidden"
        assert f"not allowed to act for user {clients['owner']}" in unbound.get_json()["reason"]
        assert "user_ids" in unbound.get_json()["reason"]

    def test_a_401_still_says_nothing_about_why(self, app_client, clients):
        resp = send(app_client, "GET", f"/api/flare-status?user_id={clients['owner']}", secret=OTHER_SECRET)
        assert resp.status_code == 401
        assert resp.get_json() == {"error": "unauthorized"}

    def test_unknown_permit_names_in_config_grant_nothing(self, app_client, clients, monkeypatch):
        import appcore
        monkeypatch.setitem(appcore.CONFIG["api_clients"], "watcher",
                            {"secret": SECRET, "permits": ["everything", "admin"],
                             "user_ids": [clients["owner"]]})
        resp = send(app_client, "GET", f"/api/flare-status?user_id={clients['owner']}", client="watcher")
        assert resp.status_code in (403, 500)

    def test_nothing_that_could_grant_the_permit_fails_closed(self, app_client, users, monkeypatch):
        import appcore
        monkeypatch.setitem(appcore.CONFIG, "api_token", "")
        monkeypatch.setitem(appcore.CONFIG, "api_clients", {
            "phone": {"secret": SECRET, "permits": ["health_sync"], "user_ids": [users["owner"]]}})
        resp = app_client.get(f"/api/flare-status?user_id={users['owner']}")
        assert resp.status_code == 500


# ------------------------------------------------------------------
# Clients with no clock (the wearable)
# ------------------------------------------------------------------

def wearable_sync(app_client, boot_id, device_ms, headers=None, client="wearable"):
    body = f"{boot_id},{max(device_ms - 1000, 0)},10,20,1,2,3900\n".encode()
    signed = api_signing.counter_headers(client, SECRET, "POST", "/api/uv/ingest",
                                         body, boot_id, device_ms)
    signed.update(headers or {})
    return app_client.post("/api/uv/ingest", data=body, headers=signed, content_type="text/csv")


class TestCounterClients:
    def test_the_counter_must_keep_going_up(self, app_client, clients):
        assert wearable_sync(app_client, 42, 5000).status_code == 200
        assert wearable_sync(app_client, 42, 5000).status_code == 401   # replayed
        assert wearable_sync(app_client, 42, 4000).status_code == 401   # older
        assert wearable_sync(app_client, 42, 6000).status_code == 200
        assert wearable_sync(app_client, 43, 1500).status_code == 200   # new boot, uptime restarts
        assert wearable_sync(app_client, 42, 9000).status_code == 401   # an earlier boot

    def test_the_counter_headers_are_signed(self, app_client, clients):
        resp = wearable_sync(app_client, 42, 5000, headers={"X-Device-Ms": "5001"})
        assert resp.status_code == 401

    def test_a_counter_client_cannot_use_a_timestamp_instead(self, app_client, clients):
        body = b"42,1000,10,20,1,2,3900\n"
        headers = api_signing.signed_headers("wearable", SECRET, "POST", "/api/uv/ingest", body)
        resp = app_client.post("/api/uv/ingest", data=body, headers=headers, content_type="text/csv")
        assert resp.status_code == 401

    def test_a_clock_client_cannot_use_a_counter_instead(self, app_client, clients):
        body = health_sync_body(clients["owner"])
        headers = api_signing.counter_headers("phone", SECRET, "POST", "/api/health-sync", body, 1, 1)
        resp = app_client.post("/api/health-sync", data=body, headers=headers,
                               content_type="application/json")
        assert resp.status_code == 401


# ------------------------------------------------------------------
# The backup routes
# ------------------------------------------------------------------

class TestBackupPermit:
    @pytest.fixture
    def single_user(self, app_client, fresh_db, monkeypatch):
        import appcore
        import db
        owner = db.create_user("owner", "Owner", "not-a-real-password-hash")
        monkeypatch.setitem(appcore.CONFIG, "api_token", "")
        monkeypatch.setitem(appcore.CONFIG, "single_user_mode", True)
        monkeypatch.setitem(appcore.CONFIG, "api_clients", signed_clients(owner))
        return app_client

    def test_a_client_with_the_backup_permit_can_export(self, single_user):
        resp = send(single_user, "GET", "/api/backup/export", client="backups")
        assert resp.status_code == 200
        assert resp.mimetype == "application/zip"

    def test_a_client_without_the_backup_permit_is_forbidden(self, single_user):
        resp = send(single_user, "GET", "/api/backup/export", client="phone")
        assert resp.status_code == 403

    @pytest.mark.parametrize("method, url", [("GET", "/api/backup/export"), ("POST", "/api/backup/restore")],
                             ids=["export", "restore"])
    def test_on_an_ordinary_server_backups_still_need_a_session(self, app_client, clients, method, url):
        resp = send(app_client, method, url, client="backups")
        assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")


# ------------------------------------------------------------------
# Legacy bearer tokens, until they are retired
# ------------------------------------------------------------------

class TestLegacyBearer:
    @pytest.fixture
    def bearer(self, app_client, clients, monkeypatch):
        import appcore
        monkeypatch.setitem(appcore.CONFIG, "api_token", "the-api-token")
        monkeypatch.setitem(appcore.CONFIG, "wearable_token", "the-wearable-token")
        return clients

    def test_each_token_opens_only_what_it_opened_before(self, app_client, bearer):
        user = bearer["owner"]
        wearable = {"Authorization": "Bearer the-wearable-token"}
        api = {"Authorization": "Bearer the-api-token"}
        assert app_client.post("/api/health-sync", headers=wearable,
                               json={"user_id": user, "steps": 1}).status_code == 401
        assert app_client.post("/api/uv/ingest", headers=api, data=b"",
                               content_type="text/csv").status_code == 401
        assert app_client.post("/api/health-sync", headers=api,
                               json={"user_id": user, "steps": 1}).status_code == 200
        assert app_client.get(f"/api/flare-status?user_id={user}", headers=api).status_code == 200

    def test_turning_bearer_off_refuses_the_right_token(self, app_client, bearer, monkeypatch):
        import appcore
        monkeypatch.setitem(appcore.CONFIG, "allow_bearer", False)
        resp = app_client.get(f"/api/flare-status?user_id={bearer['owner']}",
                              headers={"Authorization": "Bearer the-api-token"})
        assert resp.status_code == 401
