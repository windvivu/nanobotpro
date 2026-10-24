"""The fleet turn a coroutine runs for, and the envelope that names it.

A fleet turn carries an envelope in InboundMessage.metadata["_fleet"]:
{"turn_id", "session_key", "chat_id", "principal", "mode"}. While the turn runs, current_fleet_turn
holds it, and MessageBus.publish_outbound copies it onto every message the turn sends to its fleet
chat. Every send of a turn runs in the turn's task or in one the turn started (contextvars follow
asyncio tasks), so the stream, progress, error and message-tool sends all carry it without each of
them copying metadata.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

FLEET_CHANNEL = "fleet"
ENVELOPE_KEY = "_fleet"
TERMINAL_KEY = "_fleet_terminal"
# Background work (a sub-agent, a cron job) remembers the fleet turn that started it here, and a late
# reply carries it so Fleet Manager can file the result under that turn and only that fleet gets it
# (plan 3.3.11 item 3, decision 19)
ORIGIN_KEY = "_fleet_origin"

current_fleet_turn: ContextVar[dict[str, Any] | None] = ContextVar("current_fleet_turn", default=None)


def fleet_origin_now() -> dict[str, Any] | None:
    """What background work started in the current fleet turn should remember about it, or None."""
    envelope = current_fleet_turn.get()
    if envelope is None:
        return None
    return {"turn_id": envelope.get("turn_id"), "fleet_id": envelope.get("fleet_id") or "",
            "principal": envelope.get("principal"), "session_key": envelope.get("session_key"),
            "chat_id": envelope.get("chat_id")}


def origin_of(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """The fleet origin a late reply or notice carries, or None."""
    origin = (metadata or {}).get(ORIGIN_KEY)
    return origin if isinstance(origin, dict) else None


def envelope_of(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """The fleet envelope in a message's metadata, or None."""
    envelope = (metadata or {}).get(ENVELOPE_KEY)
    return envelope if isinstance(envelope, dict) and envelope.get("turn_id") else None


def stamp_envelope(msg: Any) -> None:
    """Give an outbound message the envelope of the fleet turn sending it to that turn's chat."""
    envelope = current_fleet_turn.get()
    if envelope is None or msg.channel != FLEET_CHANNEL or msg.chat_id != envelope.get("chat_id"):
        return
    if ENVELOPE_KEY not in (msg.metadata or {}):
        # A new dict: the turn's inbound metadata can be the same object
        msg.metadata = {**(msg.metadata or {}), ENVELOPE_KEY: envelope}
