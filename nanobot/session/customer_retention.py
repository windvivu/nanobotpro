"""Bound public customer session storage without touching internal sessions."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Collection
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.session.manager import SessionManager


class CustomerSessionRetention:
    INTERVAL_SECONDS = 24 * 60 * 60
    CUSTOMER_CHANNELS = frozenset({"zalo", "telegram"})
    # What the Zalo channel saves a customer sent: <customer>_<ms>.<ext> (custom)
    MEDIA_NAME = re.compile(r"(?P<principal>[^_\\/]+)_\d{1,20}\.[A-Za-z0-9]{1,8}")

    def __init__(self, sessions: SessionManager, *, enabled: bool = False,
                 retention_days: int = 180, max_sessions: int = 5000,
                 get_customer_state: Callable[[], Any] | None = None,
                 get_media_dir: Callable[[], Path | None] | None = None):
        if retention_days < 1 or max_sessions < 1:
            raise ValueError("Customer session retention limits must be positive")
        self.sessions = sessions
        self.enabled = enabled
        self.retention_days = retention_days
        self.max_sessions = max_sessions
        # Resolved lazily by the gateway to the channel's shared store (M6).
        self.get_customer_state = get_customer_state
        # The Zalo channel's media folder, set by the gateway only: without it no picture is ever removed,
        # so a test or a tool building this class cannot reach a real folder (custom)
        self.get_media_dir = get_media_dir

    @staticmethod
    def _utc(value: datetime) -> datetime:
        # Existing session timestamps are local naive datetime.now() values.
        return value.astimezone(timezone.utc)

    def _safe_file(self, path: Path, root: Path) -> bool:
        return (not path.is_symlink() and path.is_file()
                and path.resolve().parent == root)

    def _read_candidate(self, path: Path, root: Path) -> tuple[str, datetime] | None:
        if not self._safe_file(path, root):
            return None
        with path.open(encoding="utf-8") as stream:
            # Only the metadata header is needed; never load whole transcripts.
            header = json.loads(stream.readline(65536))
        if not isinstance(header, dict) or header.get("_type") != "metadata":
            return None
        key = header.get("key")
        if not isinstance(key, str):
            return None
        channel, separator, principal = key.partition(":")
        if channel not in self.CUSTOMER_CHANNELS or not separator or not principal:
            return None
        canonical = self.sessions._get_session_path(key).absolute()
        if path not in (canonical, canonical.with_suffix(".jsonl.tmp")):
            return None
        return key, self._utc(datetime.fromisoformat(header["updated_at"]))

    def _read_companion(self, path: Path, root: Path,
                        original: tuple[str, datetime] | None) -> tuple[str, datetime] | None:
        try:
            return self._read_candidate(path, root)
        except (ValueError, TypeError, KeyError):
            pass
        # A crash can leave a partially written temp file. Its canonical base
        # supplies the principal; its mtime prevents deleting a recent write.
        if original is not None and self._safe_file(path, root):
            return original[0], datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        return None

    def sweep(self, *, now: datetime | None = None,
              active_keys: Collection[str] = ()) -> int:
        """Run on the agent event loop, with no await between scan and deletion.

        Unknown/corrupt files and active sessions are preserved. Active sessions
        may temporarily put storage over the cap until a later sweep.
        """
        if not self.enabled:
            return 0
        root = self.sessions.sessions_dir.resolve()
        expected = self.sessions.workspace.resolve() / "sessions"
        if root != expected:
            logger.warning("Customer retention skipped: sessions directory leaves workspace")
            return 0
        cutoff = self._utc(now or datetime.now(timezone.utc)) - timedelta(days=self.retention_days)
        candidates: dict[str, tuple[datetime, list[Path]]] = {}
        invalid: set[Path] = set()
        bases = {p.with_suffix("") if p.name.endswith(".jsonl.tmp") else p
                 for p in root.iterdir() if p.name.endswith((".jsonl", ".jsonl.tmp"))}
        for base in bases:
            paths = [p for p in (base, base.with_suffix(".jsonl.tmp")) if p.exists() or p.is_symlink()]
            try:
                original = self._read_candidate(base, root) if base.exists() else None
                records = [original] if base.exists() else []
                temp = base.with_suffix(".jsonl.tmp")
                if temp in paths:
                    records.append(self._read_companion(temp, root, original))
                # Unknown principals and redirected companions are never deleted on a guess.
                if not records or any(record is None for record in records):
                    invalid.add(base)
                    continue
                key = records[0][0]
                if any(record[0] != key for record in records):
                    invalid.add(base)
                    continue
                updated = max(record[1] for record in records)
                cached = self.sessions._cache.get(key)
                if cached is not None:
                    updated = max(updated, self._utc(cached.updated_at))
                candidates[key] = updated, paths
            except (OSError, ValueError, TypeError, KeyError):
                invalid.add(base)

        expired = {key for key, (updated, _) in candidates.items()
                   if updated < cutoff and key not in active_keys}
        remaining = sorted((updated, key) for key, (updated, _) in candidates.items() if key not in expired)
        excess = max(0, len(remaining) - self.max_sessions)
        capped = [key for _, key in remaining if key not in active_keys][:excess]
        deleted = 0
        removed_customers: set[str] = set()
        for key in sorted(expired | set(capped)):
            paths = candidates[key][1]
            try:
                # Recheck every resolved target immediately before destructive operations.
                if any(not self._safe_file(p, root) for p in paths):
                    continue
                if key.startswith("zalo:"):
                    # Their unanswered questions go with them (custom, review 13). A locked log keeps
                    # the whole customer for the next sweep, so nothing is left behind for good.
                    from nanobot.agent.unanswered import UnansweredLog
                    if UnansweredLog(self.sessions.workspace).forget(key.partition(":")[2]) is None:
                        continue
                if key.startswith("zalo:") and self.get_customer_state is not None:
                    state = self.get_customer_state()
                    if state is not None:
                        state.forget_customer(key.partition(":")[2])
                for path in paths:
                    path.unlink()
                self.sessions.invalidate(key)
                deleted += 1
                if key.startswith("zalo:"):
                    removed_customers.add(key.partition(":")[2])
            except (OSError, ValueError):
                logger.exception("Customer retention could not remove a session")
        pictures = self._sweep_media(cutoff, removed_customers)
        logger.info("Customer retention: removed {} sessions and {} pictures; preserved {} unrecognized files",
                    deleted, pictures, len(invalid))
        return deleted

    def _sweep_media(self, cutoff: datetime, removed_customers: set[str]) -> int:
        """Pictures customers sent (custom): those of a customer removed above, and any older than the
        retention days. Only files named as the channel names them, directly in its media folder."""
        if self.get_media_dir is None:
            return 0
        try:
            media = self.get_media_dir()
            if media is None or not media.is_dir():
                return 0
            root = media.resolve()
            entries = list(root.iterdir())
        except OSError:
            logger.exception("Customer retention could not read the media folder")
            return 0
        oldest = cutoff.timestamp()
        removed = 0
        for path in entries:
            match = self.MEDIA_NAME.fullmatch(path.name)
            try:
                if not match or path.is_symlink() or not path.is_file() or path.resolve().parent != root:
                    continue
                if match["principal"] in removed_customers or path.stat().st_mtime < oldest:
                    path.unlink()
                    removed += 1
            except OSError:
                logger.exception("Customer retention could not remove a picture")
        return removed

    async def run(self, active_keys: Callable[[], Collection[str]]) -> None:
        if not self.enabled:
            return
        while True:
            try:
                self.sweep(active_keys=active_keys())
            except Exception:
                logger.exception("Customer retention sweep failed; will retry next day")
            await asyncio.sleep(self.INTERVAL_SECONDS)
