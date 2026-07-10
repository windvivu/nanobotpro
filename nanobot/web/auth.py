"""Authentication for nanobot web dashboard."""

import base64
import hashlib
import secrets
import time
from functools import wraps

from fastapi import Request
from fastapi.responses import RedirectResponse

# In-memory session store: {token: expiry_timestamp}
_sessions: dict[str, float] = {}
_SESSION_TTL = 24 * 60 * 60  # 24 hours
_COOKIE_NAME = "nanobot_session"  # base name, actual name set by init_cookie_name()


def init_cookie_name(bot_id: str = "") -> str:
    """Set cookie name unique per bot_id to avoid conflicts on same host."""
    global _COOKIE_NAME
    if bot_id:
        _COOKIE_NAME = f"nanobot_session_{bot_id}"
    return _COOKIE_NAME


def get_cookie_name() -> str:
    """Return the current cookie name."""
    return _COOKIE_NAME


def _hash_password(password: str) -> str:
    """Simple SHA-256 hash for password comparison."""
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(input_password: str, stored_password: str) -> bool:
    """Check if input password matches the stored password."""
    if not stored_password:
        return True  # No password set = no auth required
    return _hash_password(input_password) == _hash_password(stored_password)


def create_session() -> str:
    """Create a new session token."""
    token = secrets.token_hex(32)
    _sessions[token] = time.time() + _SESSION_TTL
    return token


def validate_session(token: str | None) -> bool:
    """Check if a session token is valid and not expired."""
    if not token:
        return False
    expiry = _sessions.get(token)
    if not expiry:
        return False
    if time.time() > expiry:
        _sessions.pop(token, None)
        return False
    return True


def clear_session(token: str) -> None:
    """Remove a session token."""
    _sessions.pop(token, None)


def is_authenticated(request: Request) -> bool:
    """Check if the request has a valid session cookie."""
    token = request.cookies.get(get_cookie_name())
    return validate_session(token)


def auth_required(password: str):
    """Return True if auth is required (password is set)."""
    return bool(password)


# Paths that don't require authentication (middleware won't redirect these)
# Note: these endpoints do their OWN Basic Auth check inside
PUBLIC_PATHS = {"/login", "/static", "/healthz", "/api/health", "/api/fleet/message", "/api/fleet/claim", "/api/fleet/release", "/api/fleet/config-mcp", "/api/fleet/remove-mcp", "/api/fleet/restart", "/api/password-status"}


def verify_basic_auth(authorization_header: str | None, stored_password: str) -> bool:
    """Verify HTTP Basic Auth credentials from Authorization header.

    Used by /api/health so fleet manager can access programmatically.
    Format: 'Basic base64(username:password)'
    """
    if not stored_password:
        return True  # No password configured — allow all
    if not authorization_header or not authorization_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization_header[6:]).decode("utf-8")
        _, password = decoded.split(":", 1)
        return verify_password(password, stored_password)
    except Exception:
        return False


# ── Rate Limiting for /api/health ──────────────────────────────────────────
_RATE_LIMIT_MAX = 5          # max failures per window
_RATE_LIMIT_WINDOW = 300     # 5-minute window (seconds)
_RATE_LIMIT_BLOCK = 300      # block duration (seconds)
_fail_log: dict[str, list[float]] = {}   # ip → [fail_timestamps]
_block_until: dict[str, float] = {}      # ip → unblock_timestamp


def is_rate_limited(ip: str) -> bool:
    """Return True if the IP is currently blocked due to too many failures."""
    now = time.time()
    if now < _block_until.get(ip, 0):
        return True

    # Trim old entries
    _fail_log[ip] = [t for t in _fail_log.get(ip, []) if now - t < _RATE_LIMIT_WINDOW]
    return False


def record_auth_failure(ip: str) -> None:
    """Record a failed auth attempt, block IP if threshold exceeded."""
    now = time.time()
    _fail_log.setdefault(ip, []).append(now)
    # Trim old
    _fail_log[ip] = [t for t in _fail_log[ip] if now - t < _RATE_LIMIT_WINDOW]
    if len(_fail_log[ip]) >= _RATE_LIMIT_MAX:
        _block_until[ip] = now + _RATE_LIMIT_BLOCK


def record_auth_success(ip: str) -> None:
    """Clear failure log on successful auth."""
    _fail_log.pop(ip, None)
    _block_until.pop(ip, None)



def is_public_path(path: str) -> bool:
    """Check if the path is public (no auth required)."""
    return any(path.startswith(p) for p in PUBLIC_PATHS)
