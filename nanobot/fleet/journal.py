"""Durable record of fleet turns, the channel binding and the outbox to Fleet Manager.

One SQLite file per workspace (<workspace>/fleet/fleet.sqlite3, WAL, synchronous FULL, mode 600).

- turns: a turn is written as accepted before it reaches the bus, so a retried turn_id never runs
  twice; the terminal is an UPDATE guarded by the non-terminal states, so of two endings (a reply and
  a cancel) exactly one wins. The events a terminal produces go into the outbox in the same
  transaction (plan 3.3.12: transactional outbox).
- binding: the fleet this bot is attached to and the token it signs with (plan 3.3.1, decision 17).
  It survives a restart; only a signed release or `nanobot fleet reset-binding` removes it.
- outbox: events waiting for Fleet Manager's 2xx (plan 3.3.4, 3.3.15). A row is deleted when
  delivered, deferred with a growing delay when it fails, and dead-lettered (kept 30 days) when the
  answer says retrying is pointless or after 7 days of trying.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

NON_TERMINAL = ("accepted", "queued", "running")
TERMINAL = ("completed", "failed", "cancelled")
RETENTION_SECONDS = 7 * 24 * 3600  # as long as an outbox event may wait (plan 3.3.12)
DEAD_LETTER_RETENTION_SECONDS = 30 * 24 * 3600
PENDING_MAX_AGE_SECONDS = 7 * 24 * 3600  # not delivered by then: dead-lettered, never dropped silently
INTERRUPTED = "interrupted_by_restart"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    fleet_id TEXT NOT NULL DEFAULT '',
    session_key TEXT NOT NULL,
    principal TEXT,
    mode TEXT NOT NULL DEFAULT 'sync',
    request_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    response TEXT,
    error TEXT,
    retryable INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS binding (
    one INTEGER PRIMARY KEY CHECK (one = 1),
    fleet_id TEXT NOT NULL,
    inbox_url TEXT NOT NULL,
    token TEXT NOT NULL,
    token_id INTEGER NOT NULL,
    protocol_version INTEGER NOT NULL DEFAULT 1,
    bound_at REAL NOT NULL,
    bot_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS outbox (
    event_id TEXT PRIMARY KEY,
    fleet_id TEXT NOT NULL,
    turn_id TEXT,
    origin_turn_id TEXT,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    last_status INTEGER,
    last_error TEXT,
    dead_at REAL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox (status, next_attempt_at);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

_IN_NON_TERMINAL = "(" + ", ".join(f"'{s}'" for s in NON_TERMINAL) + ")"
_IN_TERMINAL = "(" + ", ".join(f"'{s}'" for s in TERMINAL) + ")"


@dataclass(frozen=True)
class TurnRecord:
    turn_id: str
    session_key: str
    request_hash: str
    state: str
    response: str | None
    error: str | None
    retryable: bool | None
    fleet_id: str = ""

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL


@dataclass(frozen=True)
class Binding:
    """The fleet this bot is attached to and the channel token it holds.

    bot_id is the fleet's id for this bot, which the claim names: inbox events carry it as
    X-Bot-ID, because Fleet Manager looks the bot up and derives the token by that id. It
    need not be this bot's gateway.bot_id; empty when the claim did not say.
    """

    fleet_id: str
    inbox_url: str
    token: str
    token_id: int
    protocol_version: int = 1
    bot_id: str = ""

    def public(self) -> dict[str, Any]:
        """What /api/health and the CLI show: never the token."""
        return {"fleet_id": self.fleet_id, "inbox_url": self.inbox_url, "token_id": self.token_id,
                "protocol_version": self.protocol_version, "bot_id": self.bot_id}


@dataclass
class OutboxEvent:
    event_id: str
    fleet_id: str
    turn_id: str | None
    origin_turn_id: str | None
    seq: int
    kind: str
    payload: dict[str, Any]
    occurred_at: str
    attempts: int = 0
    next_attempt_at: float = 0.0
    created_at: float = 0.0


class FleetJournal:
    """The fleet tables. Opened on first use; the gateway's opening fails the turns a previous process
    left running.

    recover=False is for another process looking at a gateway's journal (`nanobot fleet status`): it
    must not run that recovery, which would fail the turns the running gateway is in the middle of,
    nor the retention sweep. Only the gateway that owns the journal recovers it.
    """

    def __init__(self, path: Path, *, recover: bool = True) -> None:
        self.path = Path(path)
        self._recover = recover
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            # FULL: a terminal and its outbox rows are on disk before the request is answered, so a power
            # cut cannot lose an event Fleet Manager will never be sent again (plan 3.3.4, decision 3)
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(_SCHEMA)
            self._migrate(conn)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            if self._recover:
                self._fail_interrupted(conn)
                now = time.time()
                conn.execute(f"DELETE FROM turns WHERE state IN {_IN_TERMINAL} AND updated_at < ?",
                             (now - RETENTION_SECONDS,))
                conn.execute("DELETE FROM outbox WHERE status = 'dead' AND dead_at < ?",
                             (now - DEAD_LETTER_RETENTION_SECONDS,))
            self._conn = conn
        return self._conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        # A P1a journal has no seq column: the per-turn event counter (plan 3.3.2)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(turns)")}
        if "seq" not in columns:
            conn.execute("ALTER TABLE turns ADD COLUMN seq INTEGER NOT NULL DEFAULT 0")
        # A binding written before claims named the fleet's id for this bot
        columns = {row[1] for row in conn.execute("PRAGMA table_info(binding)")}
        if "bot_id" not in columns:
            conn.execute("ALTER TABLE binding ADD COLUMN bot_id TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _fail_interrupted(conn: sqlite3.Connection) -> None:
        # A turn that never started (accepted, queued) is safe to send again; one that was running
        # may have run tools, so sending it again could repeat their effects (plan 3.3.12)
        conn.execute(
            f"UPDATE turns SET retryable = CASE WHEN state = 'running' THEN 0 ELSE 1 END, "
            f"state = 'failed', error = ?, updated_at = ? WHERE state IN {_IN_NON_TERMINAL}",
            (INTERRUPTED, time.time()),
        )

    def _transaction(self, work) -> Any:
        """Run work(db) inside one BEGIN IMMEDIATE ... COMMIT (the connection is in autocommit mode)."""
        db = self._db()
        db.execute("BEGIN IMMEDIATE")
        try:
            result = work(db)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return result

    # ── turns ────────────────────────────────────────────────────────

    def begin(self, turn_id: str, *, session_key: str, request_hash: str, mode: str = "sync",
              principal: str | None = None, fleet_id: str = "") -> tuple[str, TurnRecord]:
        """Record a turn as accepted: ("created" | "existing" | "conflict", record)."""
        now = time.time()
        with self._lock:
            db = self._db()
            cur = db.execute(
                "INSERT OR IGNORE INTO turns (turn_id, fleet_id, session_key, principal, mode, request_hash, "
                "state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'accepted', ?, ?)",
                (turn_id, fleet_id, session_key, principal, mode, request_hash, now, now),
            )
            record = self._get(db, turn_id)
        if cur.rowcount == 1:
            return "created", record
        if record.request_hash != request_hash:
            return "conflict", record
        return "existing", record

    def advance(self, turn_id: str, state: str, *, from_states: tuple[str, ...]) -> bool:
        """Move a turn on (queued, running) if it is still in one of from_states."""
        marks = ", ".join("?" for _ in from_states)
        with self._lock:
            cur = self._db().execute(
                f"UPDATE turns SET state = ?, updated_at = ? WHERE turn_id = ? AND state IN ({marks})",
                (state, time.time(), turn_id, *from_states),
            )
            return cur.rowcount == 1

    def finish(self, turn_id: str, state: str, *, response: str | None = None, error: str | None = None,
               events: list[dict[str, Any]] | None = None) -> bool:
        """Set the terminal state; False when the turn had already ended (another terminal won).

        events: what the turn still owes Fleet Manager (its parts and the terminal), without seq or
        event_id. They are numbered and queued in the transaction that sets the terminal, so the journal
        and the outbox never disagree (plan 3.3.12); nothing is queued when the terminal loses.
        """
        if state not in TERMINAL:
            raise ValueError(f"not a terminal state: {state}")
        now = time.time()

        def work(db: sqlite3.Connection) -> bool:
            cur = db.execute(
                f"UPDATE turns SET state = ?, response = ?, error = ?, updated_at = ? "
                f"WHERE turn_id = ? AND state IN {_IN_NON_TERMINAL}",
                (state, response, error, now, turn_id),
            )
            won = cur.rowcount == 1
            if won and events:
                fleet_id = db.execute("SELECT fleet_id FROM turns WHERE turn_id = ?", (turn_id,)).fetchone()[0]
                self._queue(db, events, now, fleet_id=fleet_id, turn_id=turn_id)
            return won

        with self._lock:
            return self._transaction(work)

    def get(self, turn_id: str) -> TurnRecord | None:
        with self._lock:
            return self._get(self._db(), turn_id)

    @staticmethod
    def _get(db: sqlite3.Connection, turn_id: str) -> TurnRecord | None:
        row = db.execute(
            "SELECT turn_id, session_key, request_hash, state, response, error, retryable, fleet_id "
            "FROM turns WHERE turn_id = ?", (turn_id,),
        ).fetchone()
        if row is None:
            return None
        retryable = None if row[6] is None else bool(row[6])
        return TurnRecord(row[0], row[1], row[2], row[3], row[4], row[5], retryable, row[7] or "")

    # ── binding ──────────────────────────────────────────────────────

    def binding(self) -> Binding | None:
        with self._lock:
            row = self._db().execute(
                "SELECT fleet_id, inbox_url, token, token_id, protocol_version, bot_id FROM binding WHERE one = 1"
            ).fetchone()
        return Binding(row[0], row[1], row[2], int(row[3]), int(row[4]), row[5] or "") if row else None

    def bind(self, binding: Binding) -> None:
        """Attach to a fleet, or take its new token: the next outbox attempt signs with it."""
        with self._lock:
            self._db().execute(
                "INSERT INTO binding (one, fleet_id, inbox_url, token, token_id, protocol_version, bound_at, bot_id) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(one) DO UPDATE SET fleet_id = excluded.fleet_id, "
                "inbox_url = excluded.inbox_url, token = excluded.token, token_id = excluded.token_id, "
                "protocol_version = excluded.protocol_version, bound_at = excluded.bound_at, "
                "bot_id = excluded.bot_id",
                (binding.fleet_id, binding.inbox_url, binding.token, binding.token_id,
                 binding.protocol_version, time.time(), binding.bot_id),
            )

    def unbind(self) -> Binding | None:
        """Drop the binding and the events still waiting for that fleet (a release means Fleet Manager
        chose to stop receiving, plan 3.3.1). Returns what was bound."""
        before = self.binding()
        if before is None:
            return None

        def work(db: sqlite3.Connection) -> None:
            db.execute("DELETE FROM binding WHERE one = 1")
            db.execute("DELETE FROM outbox WHERE fleet_id = ? AND status = 'pending'", (before.fleet_id,))

        with self._lock:
            self._transaction(work)
        return before

    # ── outbox ───────────────────────────────────────────────────────

    def enqueue(self, events: list[dict[str, Any]], *, fleet_id: str, session_key: str) -> list[str]:
        """Queue events that belong to no turn (a late reply, a notice): seq counts per session."""
        now = time.time()
        with self._lock:
            return self._transaction(
                lambda db: self._queue(db, events, now, fleet_id=fleet_id, session_counter=session_key))

    def _queue(self, db: sqlite3.Connection, events: list[dict[str, Any]], now: float, *, fleet_id: str,
               turn_id: str | None = None, session_counter: str | None = None) -> list[str]:
        """Number the events and insert them, inside the caller's transaction."""
        if turn_id is not None:
            seq = db.execute("SELECT seq FROM turns WHERE turn_id = ?", (turn_id,)).fetchone()[0]
        else:
            row = db.execute("SELECT value FROM counters WHERE name = ?", (f"session:{session_counter}",)).fetchone()
            seq = row[0] if row else 0
        ids = []
        for event in events:
            seq += 1
            event_id = uuid.uuid4().hex
            payload = {"turn_id": turn_id, "origin_turn_id": None, "attachments": [], "metadata": {},
                       **event, "event_id": event_id, "seq": seq}
            db.execute(
                "INSERT INTO outbox (event_id, fleet_id, turn_id, origin_turn_id, seq, kind, payload, occurred_at, "
                "status, attempts, next_attempt_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)",
                (event_id, fleet_id, payload["turn_id"], payload["origin_turn_id"], seq, payload["kind"],
                 json.dumps(payload, ensure_ascii=False, sort_keys=True), payload["occurred_at"], now, now),
            )
            ids.append(event_id)
        if turn_id is not None:
            db.execute("UPDATE turns SET seq = ? WHERE turn_id = ?", (seq, turn_id))
        else:
            db.execute("INSERT INTO counters (name, value) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value = ?",
                       (f"session:{session_counter}", seq, seq))
        return ids

    def pending(self, *, now: float | None = None, limit: int = 50) -> list[OutboxEvent]:
        """Events due for a delivery attempt, oldest first."""
        now = time.time() if now is None else now
        with self._lock:
            rows = self._db().execute(
                "SELECT event_id, fleet_id, turn_id, origin_turn_id, seq, kind, payload, occurred_at, attempts, "
                "next_attempt_at, created_at FROM outbox WHERE status = 'pending' AND next_attempt_at <= ? "
                "ORDER BY created_at, rowid LIMIT ?", (now, limit),
            ).fetchall()
        return [OutboxEvent(*row[:6], json.loads(row[6]), *row[7:]) for row in rows]

    def next_attempt_at(self) -> float | None:
        """When the earliest pending event is due, or None when the outbox is empty."""
        with self._lock:
            row = self._db().execute("SELECT MIN(next_attempt_at) FROM outbox WHERE status = 'pending'").fetchone()
        return row[0] if row and row[0] is not None else None

    def delivered(self, event_id: str) -> None:
        with self._lock:
            self._db().execute("DELETE FROM outbox WHERE event_id = ?", (event_id,))

    def defer(self, event_id: str, *, next_attempt_at: float, status: int | None, error: str) -> None:
        with self._lock:
            self._db().execute(
                "UPDATE outbox SET attempts = attempts + 1, next_attempt_at = ?, last_status = ?, last_error = ? "
                "WHERE event_id = ?", (next_attempt_at, status, error[:500], event_id),
            )

    def dead_letter(self, event_id: str, *, status: int | None, error: str) -> None:
        with self._lock:
            self._db().execute(
                "UPDATE outbox SET status = 'dead', dead_at = ?, attempts = attempts + 1, last_status = ?, "
                "last_error = ? WHERE event_id = ?", (time.time(), status, error[:500], event_id),
            )

    def expire_pending(self, *, now: float | None = None) -> int:
        """Dead-letter the events that have waited longer than PENDING_MAX_AGE_SECONDS."""
        now = time.time() if now is None else now
        with self._lock:
            cur = self._db().execute(
                "UPDATE outbox SET status = 'dead', dead_at = ?, last_error = 'expired' "
                "WHERE status = 'pending' AND created_at < ?", (now, now - PENDING_MAX_AGE_SECONDS),
            )
            return cur.rowcount

    def drop_fleet(self, fleet_id: str) -> int:
        """Forget the pending events of a fleet this bot no longer belongs to (410, plan 3.3.15)."""
        with self._lock:
            cur = self._db().execute("DELETE FROM outbox WHERE fleet_id = ? AND status = 'pending'", (fleet_id,))
            return cur.rowcount

    def outbox_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db().execute("SELECT status, COUNT(*) FROM outbox GROUP BY status").fetchall()
        counts = dict(rows)
        return {"pending": counts.get("pending", 0), "dead": counts.get("dead", 0)}

    def dead_letters(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db().execute(
                "SELECT event_id, kind, turn_id, last_status, last_error, dead_at FROM outbox WHERE status = 'dead' "
                "ORDER BY dead_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(zip(("event_id", "kind", "turn_id", "last_status", "last_error", "dead_at"), row))
                for row in rows]

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
