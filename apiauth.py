"""
Who is calling the API, and what they may do.

Every machine-facing endpoint is wrapped in @require_client("<permit>"). The
decorator accepts a signed request from a client in config.json
"api_clients", or, until bearer tokens are retired, a legacy bearer token.
It then checks that the client holds the permit. Inside the view,
g.api_client says who called, and user_denied(user_id) refuses a client
acting for a user it isn't bound to.

Authentication failures are deliberately vague to the caller: every one is
the same 401, so a guesser learns nothing about which part was wrong. The
reason goes to the server log instead, which is where you look when a client
can't get in.

A 403 is different: the client has already proven who it is, so the reply
says why ("has no flare_status permit", "not allowed to act for user 1").
Telling it costs nothing, and a bare "forbidden" made a misspelled user_ids
key look the same as a missing permit.
"""

import hmac
import re
import time
from dataclasses import dataclass
from functools import wraps
from typing import Optional

from flask import g, jsonify, request

import api_signing
import db
from appcore import CONFIG, app

# What each legacy bearer token opens: exactly what it opened before permits
# existed, so no client breaks while it moves over to signing.
LEGACY_BEARER = {
    "api_token": frozenset({"health_sync", "flare_status", "backup"}),
    "wearable_token": frozenset({"uv_ingest"}),
}

_SIGNATURE_RE = re.compile(r"[0-9a-f]{64}")
_DIGITS_RE = re.compile(r"[0-9]{1,19}")

DEFAULT_MAX_BODY = 1024 * 1024


@dataclass(frozen=True)
class Client:
    name: str
    permits: frozenset
    user_ids: Optional[frozenset]  # None: any user. Only legacy bearer tokens get that.

    def may_act_for(self, user_id: int) -> bool:
        return self.user_ids is None or user_id in self.user_ids


def _bearer_matches(auth: str, token: str) -> bool:
    """True if an Authorization header carries exactly `token` as a Bearer token.

    compare_digest takes the same time however much of a guess is right, so
    response timing cannot be used to recover a token one character at a time.
    Compared as bytes, so a header with non-ASCII characters is simply a
    mismatch rather than an error.
    """
    return auth.startswith("Bearer ") and hmac.compare_digest(
        auth[7:].encode("utf-8"), token.encode("utf-8"))


def _unauthorized(reason: str, client_id: str = None):
    app.logger.warning("api auth refused %s %s client=%r: %s",
                       request.method, request.path, client_id, reason)
    return None, (jsonify({"error": "unauthorized"}), 401)


def _configured_clients() -> dict:
    clients = CONFIG.get("api_clients")
    return clients if isinstance(clients, dict) else {}


def _client_from_config(name: str, entry: dict) -> Client:
    """A Client from its config entry. Anything malformed grants nothing."""
    permits = entry.get("permits")
    user_ids = entry.get("user_ids")
    return Client(
        name=name,
        permits=frozenset(p for p in permits if p in api_signing.PERMITS)
        if isinstance(permits, list) else frozenset(),
        user_ids=frozenset(u for u in user_ids if isinstance(u, int) and not isinstance(u, bool))
        if isinstance(user_ids, list) else frozenset(),
    )


def _signed(client_id: str, max_body: int):
    """Check a signed request. Returns (Client, None) or (None, error response)."""
    entry = _configured_clients().get(client_id)
    secret = entry.get("secret") if isinstance(entry, dict) else None
    if not (isinstance(secret, str) and secret):
        return _unauthorized("unknown client", client_id)

    signature = request.headers.get("X-Signature", "")
    if not _SIGNATURE_RE.fullmatch(signature):
        return _unauthorized("malformed X-Signature", client_id)

    counter = entry.get("clock") == "counter"
    if counter:
        boot_id = request.headers.get("X-Boot-Id", "")
        device_ms = request.headers.get("X-Device-Ms", "")
        if not (_DIGITS_RE.fullmatch(boot_id) and _DIGITS_RE.fullmatch(device_ms)):
            return _unauthorized("malformed X-Boot-Id or X-Device-Ms", client_id)
        stamp, nonce = f"{int(boot_id)}.{int(device_ms)}", ""
    else:
        stamp = request.headers.get("X-Timestamp", "")
        nonce = request.headers.get("X-Nonce", "")
        if not _DIGITS_RE.fullmatch(stamp):
            return _unauthorized("malformed X-Timestamp", client_id)
        if not api_signing.NONCE_RE.fullmatch(nonce):
            return _unauthorized("malformed X-Nonce", client_id)

    # The body has to be read to be hashed, so bound it first. A body with no
    # declared length (chunked upload) could be any size; none of our clients
    # send one.
    if request.content_length is None and request.headers.get("Transfer-Encoding"):
        return None, (jsonify({"error": "Content-Length required"}), 411)
    if (request.content_length or 0) > max_body:
        return None, (jsonify({"error": "body too large", "max_bytes": max_body}), 413)
    # Cached, so request.form and request.files still parse from it afterwards.
    body = request.get_data(cache=True)

    query = request.query_string.decode("utf-8", "replace")
    expected = api_signing.sign(secret, request.method, request.path, query,
                                client_id, stamp, nonce, body)
    if not hmac.compare_digest(expected, signature):
        return _unauthorized("signature mismatch", client_id)

    # Freshness is checked only once the signature is good, so unsigned noise
    # never touches the database.
    if counter:
        if not db.advance_api_counter(client_id, int(boot_id), int(device_ms)):
            return _unauthorized(f"counter {stamp} not above the last one seen", client_id)
    else:
        now = int(time.time())
        if abs(now - int(stamp)) > api_signing.WINDOW_SECONDS:
            return _unauthorized(f"timestamp {stamp} is {now - int(stamp)}s from server time", client_id)
        if not db.remember_api_nonce(client_id, nonce, now, api_signing.WINDOW_SECONDS):
            return _unauthorized("nonce already used", client_id)

    return _client_from_config(client_id, entry), None


def _authenticate(permit: str, max_body: int):
    """Returns (Client, None) or (None, error response)."""
    client_id = request.headers.get("X-Client-Id")
    if client_id is not None:
        return _signed(client_id, max_body)

    allow_bearer = CONFIG.get("allow_bearer", True)
    legacy_keys = [key for key, permits in LEGACY_BEARER.items()
                   if permit in permits and allow_bearer and CONFIG.get(key)]
    auth = request.headers.get("Authorization", "")
    for key in legacy_keys:
        if _bearer_matches(auth, CONFIG[key]):
            return Client(name=f"legacy:{key}", permits=LEGACY_BEARER[key], user_ids=None), None

    # Fail closed, and loudly, when nothing at all could grant this permit.
    anyone = legacy_keys or any(
        permit in _client_from_config(name, entry).permits
        for name, entry in _configured_clients().items() if isinstance(entry, dict))
    if not anyone:
        return None, (jsonify({"error": "API auth not configured"}), 500)
    return _unauthorized("no valid signature or bearer token")


def require_client(permit: str, max_body: int = DEFAULT_MAX_BODY, behind_login: bool = False):
    """Let a view run only for an authenticated API client holding `permit`.

    Normally this also marks the view as authenticating itself, which is what
    lets it past the login gate in appcore.require_login. With
    behind_login=True the view stays behind that gate too. The backup routes
    use it: they hand over or replace the whole record, so they stay reachable
    without a browser session only in single_user_mode, where the one account
    is signed in automatically.
    """
    if permit not in api_signing.PERMITS:
        raise ValueError(f"unknown permit: {permit}")

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            client, error = _authenticate(permit, max_body)
            if error:
                return error
            if permit not in client.permits:
                app.logger.warning("api auth refused %s %s client=%r: lacks permit %s",
                                   request.method, request.path, client.name, permit)
                return jsonify({"error": "forbidden",
                                "reason": f"client {client.name!r} has no {permit} permit"}), 403
            g.api_client = client
            return view(*args, **kwargs)
        if not behind_login:
            wrapper.authenticates_itself = True
        return wrapper
    return decorator


def user_denied(user_id: int):
    """A 403 response if the calling client may not act for `user_id`, else None."""
    client = g.api_client
    if client.may_act_for(user_id):
        return None
    app.logger.warning("api auth refused %s %s client=%r: not bound to user %s",
                       request.method, request.path, client.name, user_id)
    return jsonify({"error": "forbidden",
                    "reason": f"client {client.name!r} is not allowed to act for user {user_id} "
                              "(check user_ids in its api_clients entry)"}), 403
