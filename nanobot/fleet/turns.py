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
started the work when it remembers one (its origin), and only for the fleet that started it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
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

_SETTLED_MEMORY = 512  # recently ended turns, so a send that arrives after the terminal is dropped
# Fleet Manager takes events up to 64 KB (plan 3.3.2); a long reply is split into parts well under that,
# so the terminal itself is never refused for its size (plan 3.3.14 item 6)
PART_BYTES = 48 * 1024
TERMINAL_KIND = {"completed": "reply", "failed": "error", "cancelled": "cancelled"}


@dataclass(frozen=True)
class FleetTurnResult:
    turn_id: str
    state: str  # completed | failed | cancelled
    response: str = ""
    error: str = ""

    @classmethod
    def from_record(cls, record: TurnRecord) -> "FleetTurnResult":
        return cls(record.turn_id, record.state, record.response or "", record.error or "")


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
    """What a turn sent to its fleet chat, in order: the parts of the sync response."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self._stream: list[str] = []

    def add(self, msg: OutboundMessage) -> None:
        meta = msg.metadata or {}
        if meta.get("_stream_delta"):
            self._stream.append(msg.content or "")
            return
        if meta.get("_stream_end") or meta.get("_streamed"):
            # A streamed final repeats what the deltas already carried
            self._flush_stream()
            return
        if meta.get("_progress") or meta.get("_hold"):
            return
        if msg.content:
            self.parts.append(msg.content)

    def text(self) -> str:
        self._flush_stream()
        return "\n\n".join(self.parts)

    def _flush_stream(self) -> None:
        if self._stream:
            self.parts.append("".join(self._stream))
            self._stream = []


class FleetTurns:
    """The route, the agent loop and the fleet sink meet here."""

    def __init__(self, bus: "MessageBus", journal_path: Path) -> None:
        self.bus = bus
        self.journal = FleetJournal(journal_path)
        self.outbox = FleetOutbox(self.journal)
        self._waiters: dict[str, asyncio.Future[FleetTurnResult]] = {}
        self._replies: dict[str, _Reply] = {}
        self._settled: OrderedDict[str, None] = OrderedDict()
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
        status = {"attached": binding is not None, "token_id": binding.token_id if binding else None,
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
        """Run a fleet message as a bus turn and return its terminal.

        A turn_id seen before is not run again: its stored terminal is returned, or the request
        waits for the run already under way. Raises TurnConflict when the turn_id belongs to a
        different request, and asyncio.TimeoutError after timeout seconds (the turn keeps running).
        fleet_id names the fleet that sent the message: its events go only to that fleet's inbox.
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
        waiter = self._waiters.get(turn_id)
        if waiter is None:
            waiter = self._waiters[turn_id] = asyncio.get_running_loop().create_future()
        if status == "created":
            envelope = {"turn_id": turn_id, "session_key": session_key, "chat_id": from_bot,
                        "principal": principal, "mode": mode, "fleet_id": fleet_id}
            # session_key_override keeps Fleet Manager's session (room:<id>, fleet:<bot>); it also
            # keeps unifiedSession from folding room and Hall turns into the owner's session
            await self.bus.publish_inbound(InboundMessage(
                channel=FLEET_CHANNEL, sender_id=from_bot, chat_id=from_bot, content=content,
                media=list(media or []), metadata={ENVELOPE_KEY: envelope}, session_key_override=session_key,
            ))
        # shield: a request Fleet Manager gave up on leaves the turn and its waiter in place
        return await asyncio.wait_for(asyncio.shield(waiter), timeout)

    # ── Agent loop: the turn coordinator ───────────────────────────

    @staticmethod
    def envelope(msg: InboundMessage) -> dict[str, Any] | None:
        return envelope_of(msg.metadata) if msg.channel == FLEET_CHANNEL else None

    def enter(self, envelope: dict[str, Any]) -> bool:
        """Mark a turn queued; False when it already ended (it was cancelled while still queued)."""
        record = self.journal.get(envelope["turn_id"])
        if record is not None and record.terminal:
            return False
        self.journal.advance(envelope["turn_id"], "queued", from_states=("accepted",))
        return True

    def running(self, envelope: dict[str, Any]) -> None:
        self.journal.advance(envelope["turn_id"], "running", from_states=("accepted", "queued"))

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
        if turn_id in self._settled:
            logger.debug("[Fleet] Dropped a send that arrived after turn {} ended", turn_id)
            return
        terminal = meta.get(TERMINAL_KEY)
        if terminal is None:
            self._replies.setdefault(turn_id, _Reply()).add(msg)
            return
        self._settle(envelope, terminal)

    def _settle(self, envelope: dict[str, Any], terminal: dict[str, Any]) -> None:
        turn_id = envelope["turn_id"]
        reply = self._replies.pop(turn_id, None) or _Reply()
        state = terminal.get("state", "failed")
        # Safe text first: SQLite refuses a lone surrogate too, and the waiting request would hang
        response = _safe_text(reply.text()) if state == "completed" else ""
        error = _safe_text(terminal.get("error", ""))
        events = self._terminal_events(envelope, state, response, error)
        if self.journal.finish(turn_id, state, response=response, error=error or None, events=events):
            result = FleetTurnResult(turn_id, state, response, error)
            if events:
                self.outbox.wake()
        else:
            # Another terminal won the compare-and-set (a cancel): answer with the stored one
            record = self.journal.get(turn_id)
            result = FleetTurnResult.from_record(record) if record else FleetTurnResult(turn_id, state, response, error)
        self._settled[turn_id] = None
        while len(self._settled) > _SETTLED_MEMORY:
            self._settled.popitem(last=False)
        waiter = self._waiters.pop(turn_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(result)
        logger.info("[Fleet] Turn {} {}", turn_id, result.state)

    # ── Events for Fleet Manager's inbox (plan 3.3.2) ───────────────

    def _bound_to(self, fleet_id: str) -> bool:
        """Whether events for fleet_id have somewhere to go: the bot is bound to that very fleet."""
        binding = self.journal.binding()
        return binding is not None and bool(fleet_id) and binding.fleet_id == fleet_id

    def _terminal_events(self, envelope: dict[str, Any], state: str, response: str, error: str) -> list[dict]:
        """The parts and the terminal of a finished turn, or [] when no inbox is attached for its fleet."""
        if not self._bound_to(envelope.get("fleet_id") or ""):
            return []
        occurred = _now_iso()
        session_key = envelope.get("session_key") or f"fleet:{envelope.get('chat_id', '')}"
        kind = TERMINAL_KIND[state]
        events: list[dict[str, Any]] = []
        if state == "completed":
            *leading, last = split_parts(response)
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

    def _deliver_outside_turn(self, msg: OutboundMessage) -> None:
        """A send to the fleet chat with no turn behind it: a late reply (a sub-agent's or cron's result)
        or a notice. It goes to the inbox under the turn that started the work when the origin is known
        (plan 3.3.11 item 3); only the fleet that started it ever receives it (decision 19)."""
        meta = msg.metadata or {}
        if meta.get("_stream_delta") or meta.get("_stream_end") or meta.get("_progress") or meta.get("_hold"):
            return
        if not msg.content and not meta.get("_notice"):
            return
        origin = origin_of(meta) or {}
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


def _what(meta: dict[str, Any]) -> str:
    if meta.get("_notice"):
        return "Notice"
    return "Late reply" if meta.get("_late_reply") else "Message"
