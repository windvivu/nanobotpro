"""Request signing between a bot and Fleet Manager (custom; plan native-parity.md 3.3.13, P1b).

One scheme in both directions. The sender puts three headers on the request:

    X-Fleet-Sent-At:   unix seconds of THIS attempt; a retry gets a fresh value
    X-Fleet-Token-Id:  the generation of the channel token it signed with
    X-Fleet-Signature: v1=<hex HMAC-SHA256(token, "protocol_version\\nMETHOD\\ncanonical_path\\nsent_at\\nsha256(body)")>

The path in the string is the endpoint's canonical path (/api/fleet/inbox, /api/fleet/claim, ...), never
the URL as served: a reverse proxy may strip a prefix. The body is hashed as the raw bytes received,
never re-serialised. occurred_at inside an event may be days old (it waited in the outbox) and is no
reason to refuse; only sent_at is checked, within 5 minutes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

PROTOCOL_VERSION = 1
SIGNATURE_VERSION = "v1"
MAX_SKEW_SECONDS = 300

HEADER_SENT_AT = "X-Fleet-Sent-At"
HEADER_TOKEN_ID = "X-Fleet-Token-Id"
HEADER_SIGNATURE = "X-Fleet-Signature"
HEADER_BOT_ID = "X-Bot-ID"
HEADER_FLEET_ID = "X-Fleet-ID"

# Canonical paths, the same strings Fleet Manager signs and verifies
INBOX_PATH = "/api/fleet/inbox"


def canonical_json(payload: dict[str, Any]) -> bytes:
    """The bytes a signer sends and hashes: one deterministic serialisation."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")


def signing_string(method: str, canonical_path: str, sent_at: str, body: bytes,
                   protocol_version: int = PROTOCOL_VERSION) -> str:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    return f"{protocol_version}\n{method.upper()}\n{canonical_path}\n{sent_at}\n{body_hash}"


def sign(token: str, method: str, canonical_path: str, sent_at: str, body: bytes,
         protocol_version: int = PROTOCOL_VERSION) -> str:
    """The X-Fleet-Signature value."""
    message = signing_string(method, canonical_path, sent_at, body, protocol_version)
    digest = hmac.new(token.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def signed_headers(token: str, token_id: int, method: str, canonical_path: str, body: bytes, *,
                   sent_at: float | None = None, protocol_version: int = PROTOCOL_VERSION) -> dict[str, str]:
    """Headers for an outbound signed request. sent_at: unix seconds, now by default."""
    stamp = str(int(time.time() if sent_at is None else sent_at))
    return {
        HEADER_SENT_AT: stamp,
        HEADER_TOKEN_ID: str(token_id),
        HEADER_SIGNATURE: sign(token, method, canonical_path, stamp, body, protocol_version),
    }


def verify(token: str, token_id: int, method: str, canonical_path: str, body: bytes, headers: Any, *,
           now: float | None = None, protocol_version: int = PROTOCOL_VERSION) -> tuple[bool, str, dict[str, Any]]:
    """Check a signed request against the token the bot holds.

    Returns (ok, error, extra). error is one of missing_auth, unknown_token, clock_skew (extra carries
    server_time, so the sender can correct its clock) or bad_signature. A mapping with .get works for
    headers, so both Starlette's Headers and a plain dict do.
    """
    sent_at = headers.get(HEADER_SENT_AT)
    signature = headers.get(HEADER_SIGNATURE)
    provided_id = headers.get(HEADER_TOKEN_ID)
    if not sent_at or not signature or provided_id is None:
        return False, "missing_auth", {}
    try:
        if int(provided_id) != int(token_id):
            return False, "unknown_token", {}
        sent = float(sent_at)
    except (TypeError, ValueError):
        return False, "bad_signature", {}
    current = time.time() if now is None else now
    if abs(current - sent) > MAX_SKEW_SECONDS:
        return False, "clock_skew", {"server_time": int(current)}
    expected = sign(token, method, canonical_path, sent_at, body, protocol_version)
    if not hmac.compare_digest(signature, expected):
        return False, "bad_signature", {}
    return True, "", {}
