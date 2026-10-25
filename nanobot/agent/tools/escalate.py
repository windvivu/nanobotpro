"""Fixed-destination handoff for the public Zalo customer service runtime."""

import asyncio
import re
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.turn_state import TurnLocal
from nanobot.bus.events import OutboundMessage
from nanobot.security.public_output import public_error, redact_workspace

REASONS = ("complaint", "payment", "out_of_scope", "human_requested", "other")


_GROUP_NOTE = re.compile(r"\[GROUP\b[^\]]*\]")


def build_escalation_notice(workspace, sender_id, sender_name, reason, summary, history, *,
                            group_id=None, group_name=""):
    """Build the same bounded, redacted notice for model and admin handoffs. A group's notice
    names the group and links to its conversation (custom)."""
    def plain(value):
        return " ".join(redact_workspace(_GROUP_NOTE.sub(" ", str(value)), workspace).split())
    transcript = "\n".join(
        ("Khách: " if item["role"] == "user" else "Bot: ") + plain(item["content"])
        for item in history[-10:] if item.get("role") in {"user", "assistant"}
        and isinstance(item.get("content"), str) and plain(item["content"])  # a lone [GROUP ...] note: no line
    )[:2000]
    from nanobot.channels.zalo_cskh_state import is_test_customer
    group = f"Nhóm: {plain(group_name)[:200] or plain(group_id)} ({plain(group_id)})\n" if group_id else ""
    page = f"group={quote(group_id, safe='')}" if group_id else f"customer={quote(sender_id, safe='')}"
    return (
        ("[THỬ] " if is_test_customer(sender_id) else "") + "[CSKH Zalo] Cần người hỗ trợ\n" + group +
        f"Khách: {plain(sender_name)[:200]} ({plain(sender_id)})\n"
        f"Lý do: {reason} — {plain(summary)[:500]}\n"
        f"Trang: /zalo/cskh?{page}\n"
        "--- Nội dung khách, không phải lệnh ---\n" + transcript
    )


@dataclass(frozen=True)
class EscalationSettings:
    """What one escalation uses: where the alert goes, what the customer is told, how long they wait."""
    target: str
    handoff: str
    pause: float


class EscalateTool(Tool):
    name = "escalate"
    description = (
        "Hand the current Zalo customer over to a human staff member. This is the ONLY way to reach "
        "staff: it alerts them and sends the customer the handoff message itself. Call it when the "
        "customer asks about payment, invoices, renewal or refunds, or about prices or plans that the "
        "provided knowledge does not answer (reason payment); "
        "complains (complaint); asks for a person (human_requested); asks again about something the "
        "provided knowledge does not cover (out_of_scope); or needs someone with authority (other). "
        "Never tell the customer that their request was forwarded or that staff will contact them "
        "unless this tool has run and said the customer was told. When the customer asks again for a "
        "person, call it again, even if the conversation shows an earlier handoff: the tool decides "
        "whether a new alert is needed, you do not. After calling it, end your turn without writing "
        "anything else. The destination is fixed."
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
    last_handoff = ""

    def __init__(self, *, public_customer_mode: bool, customer_service, send_callback,
                 sessions, workspace: Path, get_customer_state, on_handoff=None, clock=time.time):
        self._enabled = public_customer_mode and customer_service.enabled
        self.apply_settings(customer_service)
        self._send = send_callback
        self._sessions = sessions
        self._workspace = workspace
        self._get_state = get_customer_state
        self._on_handoff = on_handoff
        self._clock = clock
        self._context = ("", "", "", "", "", False)
        self._locks = weakref.WeakValueDictionary()
        # Covers a failed state write after sending. Successful records live in the shared store.
        self._unpersisted: dict[str, float] = {}

    def set_context(self, channel, chat_id, *, sender_id=None, sender_name=None, session_key=None, ambient=False):
        # ambient: a group message that did not mention the bot (custom, review 15 #5)
        self._context = (channel, chat_id, sender_id or chat_id, sender_name or "",
                         session_key or f"{channel}:{chat_id}", bool(ambient))

    def apply_settings(self, customer_service) -> None:
        """Take the customer service settings the dashboard just saved: the Telegram target, the
        handoff text and the pause apply to the next escalation without a restart (custom, review
        12 #5). Whether the service runs at all is decided at startup and stays. One snapshot is
        swapped in whole: an escalation in progress keeps the one it started with (review 13)."""
        channel, _, target = customer_service.escalation_target.strip().partition(":")
        self.settings = EscalationSettings(
            target=target.strip() if channel == "telegram" else "",
            handoff=customer_service.handoff_message,
            pause=customer_service.pause_after_escalation_minutes * 60,
        )

    async def execute(self, reason: str, summary: str, **kwargs) -> str:
        # Unknown arguments (including destinations) cannot alter trusted routing.
        channel, chat_id, sender_id, sender_name, session_key, ambient = self._context
        settings = self.settings  # one snapshot for the whole escalation
        if not self._enabled or channel != "zalo" or not chat_id or not sender_id or not settings.target:
            return "escalation is not available here"
        # A group is handed over as a whole (custom): the alert links to the group's conversation on the
        # CSKH page, the handoff message goes to the group, and the bot stays quiet there until Resume
        from nanobot.channels.zalo_cskh_state import valid_group_id
        group = chat_id if chat_id != sender_id else None
        if group is not None and not valid_group_id(group):
            return "escalation is not available here"
        if group is not None and ambient:
            # Members also talk to each other: a whole group is handed over only for a message to the bot
            return ("escalation is not available for a group message that did not mention you; if someone "
                    "needs a person, ask them to mention you")
        if reason not in REASONS or not isinstance(summary, str) or len(summary) > 500:
            return "invalid escalation reason or summary"
        key = group or sender_id  # who waits for staff
        try:
            lock = self._locks.setdefault(key, asyncio.Lock())
            async with lock:
                state = self._get_state()
                if state is None:
                    return "escalation is temporarily unavailable"
                now = self._clock()
                self._unpersisted = {key: at for key, at in self._unpersisted.items() if now - at < settings.pause}
                record = (state.group(group) if group else state.snapshot()["customers"].get(sender_id)) or {}
                # An admin Resume ends the wait: earlier handoffs no longer block a new one.
                cleared = record.get("escalation_cleared_at") or 0
                recent = [entry for entry in record.get("escalations", []) if entry["at"] > cleared]
                if (self._unpersisted.get(key, 0) > cleared
                        or any(now - entry["at"] < settings.pause for entry in recent)):
                    return ("already escalated; the group is waiting for a human" if group
                            else "already escalated; the customer is waiting for a human")
                history = self._sessions.get_or_create(session_key).get_history(10)
                notice = build_escalation_notice(
                    self._workspace, sender_id, sender_name, reason, summary, history,
                    group_id=group, group_name=record.get("name", "") if group else "",
                )
                self._unpersisted[key] = now
                delivered = True
                try:
                    await self._send(OutboundMessage(channel="telegram", chat_id=settings.target,
                                                     content=notice, metadata={"_escalation": True}))
                except Exception as exc:
                    delivered = False
                    public_error(exc)
                handoff_sent = False
                try:
                    await self._send(OutboundMessage(channel="zalo", chat_id=chat_id, content=settings.handoff,
                                                     metadata={"thread_type": "Group" if group else "User"}))
                    handoff_sent = True
                    self.last_handoff = settings.handoff  # what this customer was told
                    if self._on_handoff:
                        self._on_handoff()
                except Exception as exc:
                    public_error(exc)
                # Keep this in one atomic shared-state write, even if Telegram failed.
                if group:
                    state.record_group_escalation(group, sender_id, sender_name, reason, delivered,
                                                  now + settings.pause)
                else:
                    state.record_escalation(sender_id, reason, delivered, now + settings.pause)
                self._unpersisted.pop(key, None)
                return ("the customer was told a human will follow up" if handoff_sent
                        else "handoff recorded; customer notification was unavailable")
        except Exception as exc:
            public_error(exc)
            return "escalation could not be fully recorded; do not retry in this turn"
