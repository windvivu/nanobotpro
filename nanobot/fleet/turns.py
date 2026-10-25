"""Fleet turns over the bus (custom; plan: fleet-manager native-parity.md, P1a and P1b).

POST /api/fleet/message used to call process_direct. A fleet message now runs as a bus turn, as a
channel's message does: the route records it in the journal, publishes it and waits for its
terminal. The agent loop marks the turn queued and running and, whatever path the turn takes,
publishes exactly one terminal as the turn's last outbound message. Channel "fleet" is an outbound
sink (ChannelManager.outbound_sinks): it collects what the turn sends and, on the terminal, sets the
journal by compare-and-set and answers the waiting request. The outbound queue is FIFO and drained
by one task, so every part of the turn has arrived when its terminal does.

P1b adds the way back. When the bot is bound to the fleet the turn came from (plan 3.3.1), the
terminal also becomes events in the outbox, written in the same transaction as the journal's
terminal, and FleetOutbox posts them to Fleet Manager's inbox. A send outside any turn (a sub-agent's
result, a cron result, a restart notice) becomes a late_reply or notice event: filed under the turn that
started the work when it remembers one (its origin), and only for the fleet that started it. So does a
send that outlives its turn: a task the turn started inherits its context, and its sends carry the
turn's envelope after the terminal (/dream's result).

P2 makes a turn visible while it runs: status events as it is accepted, queued and running; an ack
turn (mode "ack") answers the request at once, streams its reply as part events every second or
2 KB, sends the latest progress line at most four times a second, and ends with a terminal carrying
only what came after the last part.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.fleet.context import ENVELOPE_KEY, FLEET_CHANNEL, TERMINAL_KEY, envelope_of, origin_of
from nanobot.fleet.journal import FleetJournal, TurnRecord
from nanobot.fleet.outbox import FleetOutbox

if TYPE_CHECKING:
    from nanobot.bus.queue import MessageBus

# Fleet Manager takes events up to 64 KB (plan 3.3.2); a long reply is split into parts well under that,
# so the terminal itself is never refused for its size (plan 3.3.14 item 6)
PART_BYTES = 48 * 1024
# An ack turn streams its answer as parts: one every PART_INTERVAL seconds, or sooner at PART_FLUSH_BYTES
# of deltas; progress lines go at most one per PROGRESS_INTERVAL per turn (plan 3.3.2, P2 decisions)
PART_INTERVAL = 1.0
PART_FLUSH_BYTES = 2 * 1024
PROGRESS_INTERVAL = 0.25
TERMINAL_KIND = {"completed": "reply", "failed": "error", "cancelled": "cancelled"}


@dataclass(frozen=True)
class FleetTurnResult:
    turn_id: str
    state: str  # a sync answer: completed | failed | cancelled; an ack answer may be accepted | queued | running
    response: str = ""
    error: str = ""

    @classmethod
    def from_record(cls, record: TurnRecord) -> "FleetTurnResult":
        return cls(record.turn_id, record.state, record.response or "", record.error or "")

    @staticmethod
    def is_terminal(state: str) -> bool:
        return state in TERMINAL_KIND


class TurnConflict(Exception):
    """The turn_id is already used by a different request."""


def describe_error(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return text[:500]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe_text(text: str) -> str:
    """Text that encodes as UTF-8: a lone surrogate would make its event unsendable, and hold the
    outbox behind it, forever."""
    return text.encode("utf-8", "replace").decode("utf-8")


def _json_cost(ch: str) -> int:
    """Bytes one character takes inside a JSON string (ensure_ascii=False), escapes included."""
    if ch in '"\\':
        return 2
    if ord(ch) < 0x20:
        return 2 if ch in "\b\f\n\r\t" else 6  # \n ... or \u00XX
    return len(ch.encode("utf-8"))


def split_parts(text: str, limit: int = PART_BYTES) -> list[str]:
    """Cut text into pieces of at most limit bytes **as they are sent** — UTF-8 inside a JSON string,
    escapes counted — on character boundaries; "" stays [""]. Measured raw, a reply of newlines would
    double on the way and Fleet Manager would refuse its terminal (413)."""
    if len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2 <= limit:
        return [text]
    parts: list[str] = []
    start, size = 0, 0
    for i, ch in enumerate(text):
        cost = _json_cost(ch)
        if size + cost > limit and i > start:
            parts.append(text[start:i])
            start, size = i, 0
        size += cost
    parts.append(text[start:])
    return parts


class _Reply:
    """What a turn sent to its fleet chat, in order, as the answer reads.

    pieces: text with the separators of the sync answer already in; "".join(pieces) is the answer.
    Stream deltas gather until a flush — every PART_INTERVAL seconds, at PART_FLUSH_BYTES, or at the
    stream's end — makes one piece of them. An ack turn sends each piece on as a `part` event as it
    completes (emitted counts them), so its terminal carries only what came after (plan 3.3.14 item 6).
    progress keeps the latest progress line until the sink sends it (plan 3.3.2: at most 4 a second)."""

    def __init__(self) -> None:
        self.pieces: list[str] = []
        self.emitted = 0
        self.progress: tuple[str, bool] | None = None  # text, is a tool hint
        self._stream: list[str] = []
        self._stream_bytes = 0
        self._stream_open = False  # the running stream already made a piece: the next needs no separator

    def add(self, msg: OutboundMessage) -> bool:
        """Take a send; True when a piece completed (a whole message, or enough of a stream)."""
        meta = msg.metadata or {}
        if meta.get("_stream_delta"):
            self._stream.append(msg.content or "")
            self._stream_bytes += len((msg.content or "").encode("utf-8"))
            return self._stream_bytes >= PART_FLUSH_BYTES and self.flush_stream(keep_open=True)
        if meta.get("_stream_end") or meta.get("_streamed"):
            # A streamed final repeats what the deltas already carried
            return self.flush_stream()
        if meta.get("_progress"):
            self.progress = (msg.content or "", bool(meta.get("_tool_hint")))
            return False
        if meta.get("_hold") or not msg.content:
            return False
        self.flush_stream()
        self._add_piece(msg.content, separate=True)
        return True

    @property
    def streaming(self) -> bool:
        return bool(self._stream)

    def flush_stream(self, keep_open: bool = False) -> bool:
        """Make a piece of the deltas gathered so far; keep_open: the stream goes on after it."""
        if not self._stream:
            if not keep_open:
                self._stream_open = False
            return False
        text = "".join(self._stream)
        self._stream, self._stream_bytes = [], 0
        self._add_piece(text, separate=not self._stream_open)
        self._stream_open = keep_open
        return True

    def _add_piece(self, text: str, *, separate: bool) -> None:
        self.pieces.append(("\n\n" if separate and self.pieces else "") + text)

    def text(self) -> str:
        self.flush_stream()
        return "".join(self.pieces)

    def take_unsent(self) -> list[str]:
        """The pieces not yet sent as parts, and count them as sent."""
        unsent = self.pieces[self.emitted:]
        self.emitted = len(self.pieces)
        return unsent


class FleetTurns:
    """The route, the agent loop and the fleet sink meet here."""

    def __init__(self, bus: "MessageBus", journal_path: Path) -> None:
        self.bus = bus
        self.journal = FleetJournal(journal_path)
        self.outbox = FleetOutbox(self.journal)
        self._waiters: dict[str, asyncio.Future[FleetTurnResult]] = {}
        self._replies: dict[str, _Reply] = {}
        # Turns this process has entered and not yet ended: the only ones whose sends are part of an
        # answer. A send for any other turn comes from work that outlived it (plan 3.3.11)
        self._live: dict[str, dict[str, Any]] = {}
        self._timers: dict[tuple[str, str], asyncio.TimerHandle] = {}
        self._progress_sent: dict[str, float] = {}
        # Health counters (plan 3.3.6, 3.3.11): sends that reached the sink without a turn or an origin,
        # and results that were not sent because the bot has since joined another fleet
        self.uncorrelated = 0
        self.dropped_foreign = 0

    @property
    def bot_id(self) -> str:
        return self.outbox.bot_id

    @bot_id.setter
    def bot_id(self, value: str) -> None:
        self.outbox.bot_id = value

    def channel_status(self) -> dict[str, Any]:
        """The fleet_channel block of /api/health (plan 3.3.6)."""
        binding = self.journal.binding()
        # inbox_url: Fleet Manager compares it with the URL it would grant now and grants the channel
        # again when its public_url changed (not a secret: it is FM's own address)
        status = {"attached": binding is not None, "token_id": binding.token_id if binding else None,
                  "inbox_url": binding.inbox_url if binding else None, "active_turns": len(self._live),
                  "uncorrelated": self.uncorrelated, "dropped_foreign": self.dropped_foreign}
        status.update(self.outbox.status())
        return status

    # ── Route: POST /api/fleet/message ─────────────────────────────

    async def submit(
        self,
        *,
        content: str,
        from_bot: str,
        session_key: str,
        turn_id: str,
        principal: dict[str, Any] | None = None,
        mode: str = "sync",
        media: list[str] | None = None,
        timeout: float | None = None,
        fleet_id: str = "",
    ) -> FleetTurnResult:
        """Run a fleet message as a bus turn. mode "sync" returns its terminal; mode "ack" returns as
        soon as the turn is in the journal, with the state it is in (plan 3.3.5), and everything else
        goes to Fleet Manager's inbox.

        A turn_id seen before is not run again: its stored terminal is returned, or (sync) the request
        waits for the run already under way, or (ack) its current state is returned. Raises
        TurnConflict when the turn_id belongs to a different request, and asyncio.TimeoutError after
        timeout seconds (the turn keeps running). fleet_id names the fleet that sent the message: its
        events go only to that fleet's inbox.
        """
        request_hash = hashlib.sha256("\x1f".join((session_key, from_bot, content)).encode()).hexdigest()
        status, record = self.journal.begin(
            turn_id, session_key=session_key, request_hash=request_hash, mode=mode,
            principal=json.dumps(principal) if principal else None, fleet_id=fleet_id,
        )
        if status == "conflict":
            raise TurnConflict(turn_id)
        if record.terminal:
            return FleetTurnResult.from_record(record)
        if status == "created":
            envelope = {"turn_id": turn_id, "session_key": session_key, "chat_id": from_bot,
                        "principal": principal, "mode": mode, "fleet_id": fleet_id}
            self._status(envelope, "accepted")
            metadata: dict[str, Any] = {ENVELOPE_KEY: envelope}
            if mode == "ack":
                metadata["_wants_stream"] = True  # parts as the answer streams (plan 3.3.2)
            # session_key_override keeps Fleet Manager's session (room:<id>, fleet:<bot>); it also
            # keeps unifiedSession from folding room and Hall turns into the owner's session
            await self.bus.publish_inbound(InboundMessage(
                channel=FLEET_CHANNEL, sender_id=from_bot, chat_id=from_bot, content=content,
                media=list(media or []), metadata=metadata, session_key_override=session_key,
            ))
        if mode == "ack":
            return FleetTurnResult(turn_id, record.state)
        waiter = self._waiters.get(turn_id)
        if waiter is None:
            waiter = self._waiters[turn_id] = asyncio.get_running_loop().create_future()
        # shield: a request Fleet Manager gave up on leaves the turn and its waiter in place
        return await asyncio.wait_for(asyncio.shield(waiter), timeout)

    # ── Agent loop: the turn coordinator ───────────────────────────

    @staticmethod
    def envelope(msg: InboundMessage) -> dict[str, Any] | None:
        return envelope_of(msg.metadata) if msg.channel == FLEET_CHANNEL else None

    def enter(self, envelope: dict[str, Any]) -> bool:
        """Mark a turn queued; False when it already ended (it was cancelled while still queued)."""
        turn_id = envelope["turn_id"]
        record = self.journal.get(turn_id)
        if record is not None and record.terminal:
            return False
        self._live[turn_id] = envelope
        self.journal.advance(turn_id, "queued", from_states=("accepted",),
                             events=self._status_events(envelope, "queued"))
        self.outbox.wake()
        return True

    def running(self, envelope: dict[str, Any]) -> None:
        self.journal.advance(envelope["turn_id"], "running", from_states=("accepted", "queued"),
                             events=self._status_events(envelope, "running"))
        self.outbox.wake()

    def end(self, envelope: dict[str, Any], state: str, error: BaseException | None = None) -> None:
        """Publish the turn's terminal as its last outbound message."""
        terminal: dict[str, Any] = {"state": state}
        if error is not None:
            terminal["error"] = describe_error(error)
        # put_nowait: also called from a task being cancelled; the outbound queue is unbounded
        self.bus.outbound.put_nowait(OutboundMessage(
            channel=FLEET_CHANNEL, chat_id=envelope["chat_id"], content="",
            metadata={ENVELOPE_KEY: envelope, TERMINAL_KEY: terminal},
        ))

    # ── Sink: ChannelManager.outbound_sinks["fleet"] ───────────────

    async def deliver(self, msg: OutboundMessage) -> None:
        meta = msg.metadata or {}
        envelope = envelope_of(meta)
        if envelope is None:
            self._deliver_outside_turn(msg)
            return
        turn_id = envelope["turn_id"]
        terminal = meta.get(TERMINAL_KEY)
        if turn_id not in self._live:
            if terminal is not None:
                logger.debug("[Fleet] Dropped a terminal for turn {}, which is not running here", turn_id)
                return
            # The turn ended (or this process never ran it): the send comes from work the turn started
            # that outlived it, a late reply of that turn, not part of its answer (plan 3.3.11)
            self._deliver_outside_turn(msg, started_by=_origin_from(envelope))
            return
        if terminal is not None:
            self._settle(envelope, terminal)
            return
        reply = self._replies.setdefault(turn_id, _Reply())
        had_progress = reply.progress
        completed = reply.add(msg)
        if envelope.get("mode") != "ack":
            return
        if completed:
            self._send_parts(envelope, reply)
        elif reply.streaming:
            self._later(turn_id, "part", PART_INTERVAL, self._flush_parts)
        if reply.progress is not None and reply.progress is not had_progress:
            self._send_progress(envelope, reply)

    def _settle(self, envelope: dict[str, Any], terminal: dict[str, Any]) -> None:
        turn_id = envelope["turn_id"]
        self._cancel_timers(turn_id)
        reply = self._replies.pop(turn_id, None) or _Reply()
        state = terminal.get("state", "failed")
        # Safe text first: SQLite refuses a lone surrogate too, and the waiting request would hang
        response = _safe_text(reply.text()) if state == "completed" else ""
        error = _safe_text(terminal.get("error", ""))
        events = self._terminal_events(envelope, state, response, error, reply)
        if self.journal.finish(turn_id, state, response=response, error=error or None, events=events):
            result = FleetTurnResult(turn_id, state, response, error)
            if events:
                self.outbox.wake()
        else:
            # Another terminal won the compare-and-set (a cancel): answer with the stored one
            record = self.journal.get(turn_id)
            result = FleetTurnResult.from_record(record) if record else FleetTurnResult(turn_id, state, response, error)
        self._live.pop(turn_id, None)
        self._progress_sent.pop(turn_id, None)
        waiter = self._waiters.pop(turn_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(result)
        logger.info("[Fleet] Turn {} {}", turn_id, result.state)

    # ── Events for Fleet Manager's inbox (plan 3.3.2) ───────────────

    def _bound_to(self, fleet_id: str) -> bool:
        """Whether events for fleet_id have somewhere to go: the bot is bound to that very fleet."""
        binding = self.journal.binding()
        return binding is not None and bool(fleet_id) and binding.fleet_id == fleet_id

    @staticmethod
    def _session_of(envelope: dict[str, Any]) -> str:
        return envelope.get("session_key") or f"fleet:{envelope.get('chat_id', '')}"

    def _status_events(self, envelope: dict[str, Any], state: str) -> list[dict[str, Any]]:
        """The status event for a turn entering accepted, queued or running (plan 3.3.3)."""
        if not self._bound_to(envelope.get("fleet_id") or ""):
            return []
        return [{"kind": "status", "state": state, "content": "", "session_key": self._session_of(envelope),
                 "occurred_at": _now_iso()}]

    def _status(self, envelope: dict[str, Any], state: str) -> None:
        events = self._status_events(envelope, state)
        if events and self.journal.add_turn_events(envelope["turn_id"], events):
            self.outbox.wake()

    def _send_parts(self, envelope: dict[str, Any], reply: _Reply) -> None:
        """Send the pieces an ack turn completed so far as part events."""
        if not self._bound_to(envelope.get("fleet_id") or ""):
            reply.take_unsent()
            return
        occurred, session_key = _now_iso(), self._session_of(envelope)
        events = [{"kind": "part", "state": None, "content": chunk, "session_key": session_key,
                   "occurred_at": occurred}
                  for piece in reply.take_unsent() for chunk in split_parts(_safe_text(piece)) if chunk]
        if events and self.journal.add_turn_events(envelope["turn_id"], events):
            self.outbox.wake()

    def _flush_parts(self, turn_id: str) -> None:
        self._timers.pop((turn_id, "part"), None)
        envelope, reply = self._live.get(turn_id), self._replies.get(turn_id)
        if envelope is None or reply is None:
            return
        if reply.flush_stream(keep_open=True):
            self._send_parts(envelope, reply)

    def _send_progress(self, envelope: dict[str, Any], reply: _Reply) -> None:
        """Send the latest progress line, at most one every PROGRESS_INTERVAL seconds per turn."""
        turn_id = envelope["turn_id"]
        due = self._progress_sent.get(turn_id, 0.0) + PROGRESS_INTERVAL - time.monotonic()
        if due > 0:
            self._later(turn_id, "progress", due, self._flush_progress)
            return
        self._flush_progress(turn_id)

    def _flush_progress(self, turn_id: str) -> None:
        self._timers.pop((turn_id, "progress"), None)
        envelope, reply = self._live.get(turn_id), self._replies.get(turn_id)
        if envelope is None or reply is None or reply.progress is None:
            return
        text, tool_hint = reply.progress
        reply.progress = None
        self._progress_sent[turn_id] = time.monotonic()
        if not self._bound_to(envelope.get("fleet_id") or ""):
            return
        event = {"kind": "tool_hint" if tool_hint else "progress", "state": None, "content": _safe_text(text),
                 "session_key": self._session_of(envelope), "occurred_at": _now_iso()}
        if self.journal.add_turn_events(turn_id, [event]):
            self.outbox.wake()

    def _later(self, turn_id: str, what: str, delay: float, action) -> None:
        if (turn_id, what) in self._timers:
            return
        self._timers[(turn_id, what)] = asyncio.get_running_loop().call_later(max(delay, 0.0), action, turn_id)

    def _cancel_timers(self, turn_id: str) -> None:
        for what in ("part", "progress"):
            handle = self._timers.pop((turn_id, what), None)
            if handle is not None:
                handle.cancel()

    def _terminal_events(self, envelope: dict[str, Any], state: str, response: str, error: str,
                         reply: _Reply) -> list[dict]:
        """The parts and the terminal of a finished turn, or [] when no inbox is attached for its fleet.

        A sync turn's answer goes whole (split only for size). An ack turn already sent its pieces as
        parts; the terminal carries what came after the last one."""
        if not self._bound_to(envelope.get("fleet_id") or ""):
            return []
        occurred = _now_iso()
        session_key = self._session_of(envelope)
        kind = TERMINAL_KIND[state]
        events: list[dict[str, Any]] = []
        if state == "completed":
            remainder = _safe_text("".join(reply.take_unsent())) if envelope.get("mode") == "ack" else response
            *leading, last = split_parts(remainder)
            for part in leading:
                events.append({"kind": "part", "state": None, "content": part, "session_key": session_key,
                               "occurred_at": occurred})
            metadata = {"silent": True} if not response else {}
            events.append({"kind": kind, "state": state, "content": last, "session_key": session_key,
                           "occurred_at": occurred, "metadata": metadata})
        else:
            events.append({"kind": kind, "state": state, "content": error, "session_key": session_key,
                           "occurred_at": occurred, "metadata": {"error": error} if error else {}})
        return events

    def _deliver_outside_turn(self, msg: OutboundMessage, started_by: dict[str, Any] | None = None) -> None:
        """A send to the fleet chat with no turn behind it: a late reply (a sub-agent's or cron's result)
        or a notice. It goes to the inbox under the turn that started the work when the origin is known
        (plan 3.3.11 item 3); only the fleet that started it ever receives it (decision 19).

        started_by: the ended turn whose envelope the send carries; the origin the send names wins."""
        meta = msg.metadata or {}
        if meta.get("_stream_delta") or meta.get("_stream_end") or meta.get("_progress") or meta.get("_hold"):
            return
        if not msg.content and not meta.get("_notice"):
            return
        origin = origin_of(meta) or started_by or {}
        binding = self.journal.binding()
        if binding is None:
            logger.info("[Fleet] {} for '{}' has no fleet to go to: not bound", _what(meta), msg.chat_id)
            return
        origin_fleet = origin.get("fleet_id") or ""
        if origin_fleet and origin_fleet != binding.fleet_id:
            self.dropped_foreign += 1
            logger.warning("[Fleet] {} for '{}' was started by fleet '{}'; this bot now belongs to '{}', not sent",
                           _what(meta), msg.chat_id, origin_fleet, binding.fleet_id)
            return
        if not origin.get("turn_id"):
            self.uncorrelated += 1
        session_key = origin.get("session_key") or f"fleet:{msg.chat_id}"
        kind = "notice" if meta.get("_notice") else "late_reply"
        occurred = _now_iso()
        # A long sub-agent result is cut like a long reply: no event may exceed what the inbox takes
        pieces = split_parts(_safe_text(msg.content or ""))
        events = []
        for n, piece in enumerate(pieces, 1):
            metadata: dict[str, Any] = {"chat_id": msg.chat_id}
            if len(pieces) > 1:
                metadata.update(part=n, parts=len(pieces))
            events.append({"kind": kind, "state": None, "content": piece, "session_key": session_key,
                           "occurred_at": occurred, "origin_turn_id": origin.get("turn_id"), "metadata": metadata})
        self.journal.enqueue(events, fleet_id=binding.fleet_id, session_key=session_key)
        self.outbox.wake()
        logger.info("[Fleet] {} for '{}' queued for Fleet Manager{}", _what(meta), msg.chat_id,
                    f" (origin turn {origin['turn_id']})" if origin.get("turn_id") else " (no origin turn)")


def _origin_from(envelope: dict[str, Any]) -> dict[str, Any]:
    """The origin a turn's envelope implies for work it started (as fleet_origin_now would give)."""
    return {"turn_id": envelope.get("turn_id"), "fleet_id": envelope.get("fleet_id") or "",
            "principal": envelope.get("principal"), "session_key": envelope.get("session_key"),
            "chat_id": envelope.get("chat_id")}


def _what(meta: dict[str, Any]) -> str:
    if meta.get("_notice"):
        return "Notice"
    return "Late reply" if meta.get("_late_reply") else "Message"
