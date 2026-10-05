"""Authentication for nanobot web dashboard."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from fastapi import Request

# Session store: {sha256(token): expiry_timestamp}; the cookie carries the token itself. In memory, and
# also in a file once keep_sessions_in() is called, so logins survive a gateway restart (custom)
_sessions: dict[str, float] = {}
_session_file: Path | None = None
_password_mark = ""  # which password the logins in the file were made with
_SESSION_TTL = 24 * 60 * 60  # 24 hours
_COOKIE_NAME = "nanobot_session"  # base name, actual name set by init_cookie_name()
_CSRF_COOKIE_NAME = "nanobot_csrf"


def init_cookie_name(bot_id: str = "") -> str:
    """Set cookie name unique per bot_id to avoid conflicts on same host."""
    global _COOKIE_NAME, _CSRF_COOKIE_NAME
    if bot_id:
        _COOKIE_NAME = f"nanobot_session_{bot_id}"
        _CSRF_COOKIE_NAME = f"nanobot_csrf_{bot_id}"
    return _COOKIE_NAME


def get_cookie_name() -> str:
    """Return the current cookie name."""
    return _COOKIE_NAME


def get_csrf_cookie_name() -> str:
    """Return the browser-readable CSRF cookie name for the current bot."""
    return _CSRF_COOKIE_NAME


def create_csrf_token() -> str:
    """Create a synchronizer token for a logged-in browser session."""
    return secrets.token_urlsafe(32)


def csrf_valid(request: Request) -> bool:
    """Check the double-submit CSRF cookie/header pair."""
    cookie = request.cookies.get(get_csrf_cookie_name(), "")
    header = request.headers.get("x-csrf-token", "")
    return bool(cookie and header and hmac.compare_digest(cookie, header))


def _hash_password(password: str) -> str:
    """Simple SHA-256 hash for password comparison."""
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(input_password: str, stored_password: str) -> bool:
    """Check if input password matches the stored password."""
    if not stored_password:
        return True  # No password set = no auth required
    return _hash_password(input_password) == _hash_password(stored_password)


def _session_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def keep_sessions_in(path: Path, password: str) -> None:
    """Keep logins in `path` too, so the restart button, which starts a new process, does not log
    everyone out. The file holds hashes of the tokens, never the tokens. Logins made with another
    password are not restored: after changing the password, a restart logs everyone out (custom)."""
    global _session_file, _password_mark
    _session_file = path
    _password_mark = hmac.new(b"nanobot-web-logins", password.encode(), hashlib.sha256).hexdigest()
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(stored, dict) or stored.get("password") != _password_mark:
        return
    now = time.time()
    for key, expiry in (stored.get("sessions") or {}).items():
        if isinstance(key, str) and isinstance(expiry, (int, float)) and expiry > now:
            _sessions[key] = float(expiry)


def _save_sessions() -> None:
    if _session_file is None:
        return
    now = time.time()
    live = {key: expiry for key, expiry in _sessions.items() if expiry > now}
    try:
        _session_file.parent.mkdir(parents=True, exist_ok=True)
        temp = _session_file.with_name(_session_file.name + ".tmp")
        temp.write_text(json.dumps({"password": _password_mark, "sessions": live}), encoding="utf-8")
        temp.replace(_session_file)
    except OSError:
        pass  # logins still work; they just will not survive a restart


def create_session() -> str:
    """Create a new session token."""
    token = secrets.token_hex(32)
    _sessions[_session_key(token)] = time.time() + _SESSION_TTL
    _save_sessions()
    return token


def validate_session(token: str | None) -> bool:
    """Check if a session token is valid and not expired."""
    if not token:
        return False
    key = _session_key(token)
    expiry = _sessions.get(key)
    if not expiry:
        return False
    if time.time() > expiry:
        _sessions.pop(key, None)
        return False
    return True


def clear_session(token: str) -> None:
    """Remove a session token."""
    _sessions.pop(_session_key(token), None)
    _save_sessions()


def is_authenticated(request: Request) -> bool:
    """Check if the request has a valid session cookie."""
    token = request.cookies.get(get_cookie_name())
    return validate_session(token)


def auth_required(password: str):
    """Return True if auth is required (password is set)."""
    return bool(password)


# Paths that don't require authentication (middleware won't redirect these)
# Note: these endpoints do their OWN Basic Auth check inside
# /api/password-status is not public (custom): the login page showed the default password to anyone
PUBLIC_PATHS = {"/login", "/static", "/healthz", "/api/health", "/api/fleet/message", "/api/fleet/claim", "/api/fleet/release", "/api/fleet/config-mcp", "/api/fleet/remove-mcp", "/api/fleet/restart"}


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
