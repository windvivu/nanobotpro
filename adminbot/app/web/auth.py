"""Authentication helpers for Adminbot web UI."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from adminbot.app.utils import atomic_write_json, utc_now_iso

DEFAULT_PASSWORD = "abc123"
MIN_PASSWORD_LENGTH = 8
SESSION_COOKIE = "adminbot_session"
_PBKDF2_ITERATIONS = 210_000
LOGIN_RATE_LIMIT_ATTEMPTS = 5
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 300
LOGIN_RATE_LIMIT_LOCK_SECONDS = 900


@dataclass(slots=True)
class AuthState:
    version: int
    password_hash: str
    salt: str
    is_default_password: bool
    updated_at: str


@dataclass(slots=True)
class _LoginAttempt:
    failures: list[float] = field(default_factory=list)
    locked_until: float = 0.0


def _hash_password(password: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        _PBKDF2_ITERATIONS,
    )
    return digest.hex()


def _new_salt() -> str:
    return secrets.token_hex(16)


def _state_from_password(password: str, *, is_default_password: bool) -> AuthState:
    salt = _new_salt()
    return AuthState(
        version=1,
        password_hash=_hash_password(password, salt),
        salt=salt,
        is_default_password=is_default_password,
        updated_at=utc_now_iso(),
    )


class AdminbotAuthStore:
    """Persistent password hash store under `.adminbot/auth.json`."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def ensure_state(self) -> AuthState:
        if self.path.exists():
            return self.load()
        state = _state_from_password(DEFAULT_PASSWORD, is_default_password=True)
        self.save(state)
        return state

    def load(self) -> AuthState:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return AuthState(
            version=int(raw.get("version", 1)),
            password_hash=str(raw["password_hash"]),
            salt=str(raw["salt"]),
            is_default_password=bool(raw.get("is_default_password", False)),
            updated_at=str(raw.get("updated_at") or ""),
        )

    def save(self, state: AuthState) -> None:
        atomic_write_json(
            self.path,
            {
                "version": state.version,
                "password_hash": state.password_hash,
                "salt": state.salt,
                "is_default_password": state.is_default_password,
                "updated_at": state.updated_at,
            },
        )
        if os.name != "nt":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def verify(self, password: str) -> bool:
        state = self.ensure_state()
        candidate = _hash_password(password, state.salt)
        return secrets.compare_digest(candidate, state.password_hash)

    def change_password(self, current_password: str, new_password: str) -> None:
        if not self.verify(current_password):
            raise ValueError("Current password is incorrect.")
        if new_password == DEFAULT_PASSWORD:
            raise ValueError("Choose a password different from the default.")
        if len(new_password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
        self.save(_state_from_password(new_password, is_default_password=False))


class SessionStore:
    """Small in-memory session store. Restarting Adminbot logs users out."""

    def __init__(self) -> None:
        self._sessions: set[str] = set()

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions.add(token)
        return token

    def valid(self, token: str | None) -> bool:
        return bool(token and token in self._sessions)

    def remove(self, token: str | None) -> None:
        if token:
            self._sessions.discard(token)

    def rotate(self, token: str | None) -> str:
        self.remove(token)
        return self.create()


class LoginRateLimiter:
    """In-memory login throttle keyed by client and username."""

    def __init__(
        self,
        *,
        max_attempts: int = LOGIN_RATE_LIMIT_ATTEMPTS,
        window_seconds: int = LOGIN_RATE_LIMIT_WINDOW_SECONDS,
        lock_seconds: int = LOGIN_RATE_LIMIT_LOCK_SECONDS,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lock_seconds = lock_seconds
        self._now = now
        self._attempts: dict[str, _LoginAttempt] = {}

    def _key(self, client_host: str, username: str) -> str:
        return f"{client_host}|{username.strip().lower()}"

    def is_locked(self, client_host: str, username: str) -> bool:
        key = self._key(client_host, username)
        attempt = self._attempts.get(key)
        if attempt is None:
            return False
        now = self._now()
        if attempt.locked_until <= now:
            if attempt.locked_until:
                self._attempts.pop(key, None)
            return False
        return True

    def record_failure(self, client_host: str, username: str) -> None:
        key = self._key(client_host, username)
        now = self._now()
        attempt = self._attempts.setdefault(key, _LoginAttempt())
        cutoff = now - self.window_seconds
        attempt.failures = [timestamp for timestamp in attempt.failures if timestamp >= cutoff]
        attempt.failures.append(now)
        if len(attempt.failures) >= self.max_attempts:
            attempt.locked_until = now + self.lock_seconds

    def clear(self, client_host: str, username: str) -> None:
        self._attempts.pop(self._key(client_host, username), None)
