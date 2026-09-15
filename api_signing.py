"""
Signed API requests: how a client signs one, and the rules the server checks
it by. Standard library only, so a client written in Python can copy this file
as it is.

A signature proves three things without the secret ever crossing the wire:
the sender holds the client's secret, nothing in the request was changed on
the way (method, path, query, body), and the request is fresh (a timestamp and
a one-time nonce, or for a device with no clock, a counter that only goes up).

The wire format is written out in DEVELOPING.md. Ports to other languages
should reproduce the known-answer vectors in tests/test_api_auth.py before
talking to a real server; the iOS app's RequestSigner.swift does.
"""

import hashlib
import hmac
import re
import secrets
import time
from urllib.parse import quote, unquote_plus

VERSION = "TR1"

# How far a timestamp may be from the server's clock, either way.
WINDOW_SECONDS = 300

NONCE_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")

# Everything a client can be allowed to do. A permit names a kind of access,
# not an endpoint: one permit may cover several endpoints.
PERMITS = frozenset({
    "health_sync",   # POST /api/health-sync
    "flare_status",  # GET /api/flare-status
    "uv_ingest",     # POST /api/uv/ingest
    "backup",        # /api/backup/export and /api/backup/restore, single_user_mode only
})


def canonical_query(raw: str) -> str:
    """One spelling for a query string, so client and server hash the same text.

    `b=2&a=1`, `a=1&b=2` and `a=%31&b=2` all become `a=1&b=2`. `+` decodes to a
    space, as in any URL query, and is re-encoded as %20.
    """
    pairs = []
    for part in raw.split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        pairs.append((quote(unquote_plus(key), safe=""), quote(unquote_plus(value), safe="")))
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


def string_to_sign(method: str, path: str, query: str, client_id: str,
                   stamp: str, nonce: str, body: bytes) -> str:
    """The exact text that gets signed: eight lines, no trailing newline."""
    return "\n".join([
        VERSION,
        method.upper(),
        path,
        canonical_query(query),
        client_id,
        stamp,
        nonce,
        hashlib.sha256(body).hexdigest(),
    ])


def sign(secret: str, method: str, path: str, query: str, client_id: str,
         stamp: str, nonce: str, body: bytes) -> str:
    """Lowercase hex HMAC-SHA256 of string_to_sign, keyed with the client's secret."""
    text = string_to_sign(method, path, query, client_id, stamp, nonce, body)
    return hmac.new(secret.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).hexdigest()


def signed_headers(client_id: str, secret: str, method: str, url: str,
                   body: bytes = b"", now: int = None, nonce: str = None) -> dict:
    """Headers for a request from a client with a clock.

    `url` is the path plus any query string, e.g. "/api/flare-status?user_id=1".
    Send `body` byte for byte as signed: re-serialising JSON after signing
    changes the hash.
    """
    path, _, query = url.partition("?")
    stamp = str(int(time.time()) if now is None else now)
    nonce = nonce or secrets.token_urlsafe(24)
    return {
        "X-Client-Id": client_id,
        "X-Timestamp": stamp,
        "X-Nonce": nonce,
        "X-Signature": sign(secret, method, path, query, client_id, stamp, nonce, body),
    }


def counter_headers(client_id: str, secret: str, method: str, url: str,
                    body: bytes, boot_id: int, device_ms: int) -> dict:
    """Headers for a device with no clock, which signs its boot id and uptime instead.

    The server accepts each (boot_id, device_ms) pair only if it is larger than
    the last one it saw from this client.
    """
    path, _, query = url.partition("?")
    stamp = f"{boot_id}.{device_ms}"
    return {
        "X-Client-Id": client_id,
        "X-Boot-Id": str(boot_id),
        "X-Device-Ms": str(device_ms),
        "X-Signature": sign(secret, method, path, query, client_id, stamp, "", body),
    }
