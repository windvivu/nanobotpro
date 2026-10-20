"""Hold messages for the customer service bot (custom).

When an answer is slow, the customer first gets a short "let me check" from a library of phrases,
so the wait feels shorter. A turn holds when the model searches the knowledge (one more model
round is certain) or when no answer is ready a few seconds after the message arrived (the model is
slow, or the bot is busy with other customers). At most one hold per turn, and never two in a row:
each turn starts with a real message from the customer. Nothing is sent for an "ok" or a "thanks",
which the bot may choose not to answer, nor to a group the bot only listens to.
"""

from __future__ import annotations

import asyncio
import random
import re
import unicodedata
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage

# True to what the bot is doing: it checks; it is not a person and promises no time
DEFAULT_HOLD_MESSAGES = (
    "Dạ anh/chị chờ em chút, em kiểm tra thông tin ạ.",
    "Dạ để em xem giúp anh/chị nhé.",
    "Em tra lại tài liệu cho chính xác, anh/chị đợi em xíu ạ.",
    "Dạ anh/chị đợi em một chút, em đang tìm thông tin ạ.",
    "Để em kiểm tra lại cho chắc, anh/chị chờ em chút nhé.",
    "Dạ em đang xem thông tin, anh/chị đợi em xíu ạ.",
    "Anh/chị chờ em một lát, em lấy thông tin cho mình ạ.",
    "Dạ vâng, em xem ngay cho anh/chị ạ.",
    "Em đang tra cứu, anh/chị vui lòng chờ em chút ạ.",
    "Dạ để em tìm thông tin chính xác cho anh/chị nhé.",
    "Anh/chị đợi em chút xíu, em kiểm tra rồi trả lời ngay ạ.",
    "Dạ em nhận được rồi ạ, em xem thông tin cho anh/chị nhé.",
    "Em xem lại cho đúng rồi trả lời anh/chị ngay ạ.",
    "Dạ anh/chị chờ em giây lát ạ.",
    "Để em rà lại thông tin, anh/chị đợi em chút nhé.",
)
HOLD_MESSAGE_MAX_CHARS = 300
LAST_PHRASES_MAX = 5000  # chats whose last phrase is remembered; older ones may hear it again (review 14)

# Words of an acknowledgement or a goodbye, with and without tone marks
_ACK_WORDS = frozenset("""
    ok oke okay okie oki okela okê ô kê vâng vang dạ da ừ u ừm um uh à a ạ cảm cám cam ơn on thanks thank
    thx tks you được duoc rồi roi em anh chị chi bạn ban nhé nhe nha nhá bye tạm tam biệt biet chào chao
    hiểu hieu rõ ro yes good tốt tot hay quá qua
""".split())
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def is_acknowledgement(text: Any) -> bool:
    """True for "ok em", "cảm ơn nhé", "👍": nothing the bot needs to look up."""
    words = _WORD.findall(unicodedata.normalize("NFC", str(text or "")).casefold())
    return all(word in _ACK_WORDS for word in words)


@dataclass
class _Turn:
    slow: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    sent: dict | None = None
    answered: bool = False
    token: Any = None


_current: ContextVar[_Turn | None] = ContextVar("cskh_hold_turn", default=None)


def signal_slow() -> None:
    """The turn needs one more model round (the model searches the knowledge): hold now."""
    turn = _current.get()
    if turn is not None:
        turn.slow.set()


def signal_answered() -> None:
    """The customer got a real answer during the turn (the message tool, a handoff) while the model
    may still run: no hold after it, it would read as the bot starting over (review 14)."""
    turn = _current.get()
    if turn is not None:
        turn.answered = True
        if turn.task is not None and not turn.task.done():
            turn.task.cancel()


class HoldMessages:
    """Hold settings from channels.zalo.customerService, applied live from the CSKH page."""

    def __init__(self, send: Callable[[OutboundMessage], Awaitable[None]], service: Any = None,
                 rng: random.Random | None = None):
        self._send = send
        self._rng = rng or random.Random()
        self._last: OrderedDict[str, str] = OrderedDict()  # chat -> its last phrase, not sent twice in a row
        # chat -> whether its last slow turn got a hold: the next slow turn then gets none
        self._held: OrderedDict[str, bool] = OrderedDict()
        self.apply_settings(service)

    def apply_settings(self, service: Any) -> None:
        self.enabled = bool(getattr(service, "hold_enabled", True))
        self.after = float(getattr(service, "hold_after_seconds", 5))
        # Not every slow turn holds, or it reads as a reflex (custom): a chance at the delay, never two
        # slow turns in a row for one chat, and always once the wait reaches the safety mark (0: off)
        self.chance = min(100, max(0, int(getattr(service, "hold_probability", 50)))) / 100
        self.safety = float(getattr(service, "hold_safety_seconds", 15))
        phrases = [" ".join(str(m).split())[:HOLD_MESSAGE_MAX_CHARS]
                   for m in getattr(service, "hold_messages", None) or []]
        self.messages = tuple(m for m in phrases if m) or DEFAULT_HOLD_MESSAGES

    def wanted(self, msg: InboundMessage) -> bool:
        meta = msg.metadata or {}
        return (self.enabled and msg.channel == "zalo" and not meta.get("_held")
                and not (meta.get("thread_type") == "Group" and meta.get("group_ambient"))
                and isinstance(msg.content, str) and not is_acknowledgement(msg.content))

    def begin(self, msg: InboundMessage) -> _Turn | None:
        """Watch this turn: hold once it is slow. Call end() when its answer is ready."""
        if not self.wanted(msg):
            return None
        turn = _Turn()
        turn.token = _current.set(turn)
        turn.task = asyncio.create_task(self._watch(msg, turn))
        return turn

    def end(self, turn: _Turn | None) -> dict | None:
        """Stop watching; the hold this turn sent ({text, after_ms}), or None."""
        if turn is None:
            return None
        if turn.task is not None and not turn.task.done():
            turn.task.cancel()  # the answer came first: no hold
        try:
            _current.reset(turn.token)
        except ValueError:
            _current.set(None)  # ended in another context
        return turn.sent

    @staticmethod
    def _remember(store: OrderedDict, chat_id: str, value) -> None:
        store[chat_id] = value
        store.move_to_end(chat_id)
        while len(store) > LAST_PHRASES_MAX:
            store.popitem(last=False)

    def _pick(self, chat_id: str) -> str:
        choices = [m for m in self.messages if m != self._last.get(chat_id)] or list(self.messages)
        phrase = self._rng.choice(choices)
        self._remember(self._last, chat_id, phrase)
        return phrase

    async def _watch(self, msg: InboundMessage, turn: _Turn) -> None:
        waited = (datetime.now() - msg.timestamp).total_seconds()
        try:
            await asyncio.wait_for(turn.slow.wait(), timeout=max(0.0, self.after - waited))
        except asyncio.TimeoutError:
            pass
        if turn.answered:
            return
        # The turn is slow: hold by chance, and not if this chat's last slow turn held
        if self._held.get(msg.chat_id) or self._rng.random() >= self.chance:
            self._remember(self._held, msg.chat_id, False)
            if self.safety <= 0:
                return
            waited = (datetime.now() - msg.timestamp).total_seconds()
            await asyncio.sleep(max(0.0, self.safety - waited))  # a wait this long is not left silent
            if turn.answered:
                return
        text = self._pick(msg.chat_id)
        after_ms = max(0, round((datetime.now() - msg.timestamp).total_seconds() * 1000))
        try:
            await self._send(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=text,
                metadata={"thread_type": (msg.metadata or {}).get("thread_type", "User"), "_hold": True},
            ))
        except Exception as exc:
            logger.warning("Hold message not sent: {}", type(exc).__name__)
            return
        # Handed to the channel; the bridge drops a hold rather than sending it late (review 14)
        turn.sent = {"text": text, "after_ms": after_ms}
        self._remember(self._held, msg.chat_id, True)
        logger.debug("Hold message queued for {} after {} ms", msg.chat_id, after_ms)
