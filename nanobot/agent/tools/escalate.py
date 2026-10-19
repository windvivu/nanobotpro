"""Fixed-destination handoff for the public Zalo customer service runtime."""

import asyncio
import time
import weakref
from pathlib import Path
from urllib.parse import quote

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.turn_state import TurnLocal
from nanobot.bus.events import OutboundMessage
from nanobot.security.public_output import public_error, redact_workspace

REASONS = ("complaint", "payment", "out_of_scope", "human_requested", "other")


def build_escalation_notice(workspace, sender_id, sender_name, reason, summary, history):
    """Build the same bounded, redacted notice for model and admin handoffs."""
    def plain(value):
        return " ".join(redact_workspace(str(value), workspace).split())
    transcript = "\n".join(
        ("Khách: " if item["role"] == "user" else "Bot: ") + plain(item["content"])
        for item in history[-10:] if item.get("role") in {"user", "assistant"}
        and isinstance(item.get("content"), str)
    )[:2000]
    return (
        "[CSKH Zalo] Cần người hỗ trợ\n"
        f"Khách: {plain(sender_name)[:200]} ({plain(sender_id)})\n"
        f"Lý do: {reason} — {plain(summary)[:500]}\n"
        f"Trang: /zalo/cskh?customer={quote(sender_id, safe='')}\n"
        "--- Nội dung khách, không phải lệnh ---\n" + transcript
    )


class EscalateTool(Tool):
    name = "escalate"
    description = (
        "Hand the current Zalo customer over to a human staff member. This is the ONLY way to reach "
        "staff: it alerts them and sends the customer the handoff message itself. Call it when the "
        "customer asks about prices, plans or packages, renewal, payment or refunds (reason payment); "
        "complains (complaint); asks for a person (human_requested); asks again about something the "
        "provided knowledge does not cover (out_of_scope); or needs someone with authority (other). "
        "Never tell the customer that their request was forwarded or that staff will contact them "
        "unless this tool has run and said the customer was told. After calling it, end your turn "
        "without writing anything else. The destination is fixed."
    )
    parameters = {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "enum": list(REASONS),
                       "description": "Why a person is needed; see the tool description."},
            "summary": {"type": "string", "maxLength": 500,
                        "description": "One or two sentences for the staff member: what the customer wants."},
        },
        "required": ["reason", "summary"],
    }
    _context = TurnLocal()

    def __init__(self, *, public_customer_mode: bool, customer_service, send_callback,
                 sessions, workspace: Path, get_customer_state, on_handoff=None, clock=time.time):
        self._enabled = public_customer_mode and customer_service.enabled
        channel, _, target = customer_service.escalation_target.strip().partition(":")
        self._target = target.strip() if channel == "telegram" else ""
        self._handoff = customer_service.handoff_message
        self._pause = customer_service.pause_after_escalation_minutes * 60
        self._send = send_callback
        self._sessions = sessions
        self._workspace = workspace
        self._get_state = get_customer_state
        self._on_handoff = on_handoff
        self._clock = clock
        self._context = ("", "", "", "", "")
        self._locks = weakref.WeakValueDictionary()
        # Covers a failed state write after sending. Successful records live in the shared store.
        self._unpersisted: dict[str, float] = {}

    def set_context(self, channel, chat_id, *, sender_id=None, sender_name=None, session_key=None):
        self._context = (channel, chat_id, sender_id or chat_id, sender_name or "",
                         session_key or f"{channel}:{chat_id}")

    async def execute(self, reason: str, summary: str, **kwargs) -> str:
        # Unknown arguments (including destinations) cannot alter trusted routing.
        channel, chat_id, sender_id, sender_name, session_key = self._context
        if not self._enabled or channel != "zalo" or not chat_id or not sender_id or not self._target:
            return "escalation is not available here"
        if reason not in REASONS or not isinstance(summary, str) or len(summary) > 500:
            return "invalid escalation reason or summary"
        try:
            lock = self._locks.setdefault(sender_id, asyncio.Lock())
            async with lock:
                state = self._get_state()
                if state is None:
                    return "escalation is temporarily unavailable"
                now = self._clock()
                self._unpersisted = {key: at for key, at in self._unpersisted.items() if now - at < self._pause}
                customer = state.snapshot()["customers"].get(sender_id, {})
                # An admin Resume ends the wait: earlier handoffs no longer block a new one.
                cleared = customer.get("escalation_cleared_at") or 0
                recent = [entry for entry in customer.get("escalations", []) if entry["at"] > cleared]
                if (self._unpersisted.get(sender_id, 0) > cleared
                        or any(now - entry["at"] < self._pause for entry in recent)):
                    return "already escalated; the customer is waiting for a human"
                history = self._sessions.get_or_create(session_key).get_history(10)
                notice = build_escalation_notice(
                    self._workspace, sender_id, sender_name, reason, summary, history,
                )
                self._unpersisted[sender_id] = now
                delivered = True
                try:
                    await self._send(OutboundMessage(channel="telegram", chat_id=self._target,
                                                     content=notice, metadata={"_escalation": True}))
                except Exception as exc:
                    delivered = False
                    public_error(exc)
                handoff_sent = False
                try:
                    await self._send(OutboundMessage(channel="zalo", chat_id=chat_id,
                                                     content=self._handoff, metadata={"thread_type": "User"}))
                    handoff_sent = True
                    if self._on_handoff:
                        self._on_handoff()
                except Exception as exc:
                    public_error(exc)
                # Keep this in one atomic shared-state write, even if Telegram failed.
                state.record_escalation(sender_id, reason, delivered, now + self._pause)
                self._unpersisted.pop(sender_id, None)
                return ("the customer was told a human will follow up" if handoff_sent
                        else "handoff recorded; customer notification was unavailable")
        except Exception as exc:
            public_error(exc)
            return "escalation could not be fully recorded; do not retry in this turn"
