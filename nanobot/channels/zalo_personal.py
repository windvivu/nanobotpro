"""Zalo personal channel — unofficial, uses zca-js (Node.js) bridge."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
import time
import unicodedata
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Literal

from loguru import logger
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base

# Source files bundled with nanobot
_BRIDGE_DIR = Path(__file__).parent
_BRIDGE_JS = _BRIDGE_DIR / "zalo_bridge.js"
_BRIDGE_PKG = _BRIDGE_DIR / "zalo_bridge_package.json"
# Marker in the bridge dir: user pressed Disconnect → no auto-connect on start
_MANUAL_DISCONNECT_FLAG = "manual_disconnect.flag"


def _is_mentioned(content: str, bot_user_id: str, bot_name: str,
                  mentioned_ids: list[str], quoted_sender_id: str | None = None) -> bool:
    """Return True if the bot appears to be @mentioned or replied-to in a group message."""
    # 1. Explicit mention via userId (from zca-js mentions map)
    if bot_user_id and bot_user_id in mentioned_ids:
        return True
    # 2. Name mention in content text (e.g. "@Nanobot" or "@nanobot")
    if bot_name:
        lower = content.lower()
        if f"@{bot_name.lower()}" in lower or bot_name.lower() in lower[:40]:
            return True
    # 3. Reply/quote to bot's own message
    if bot_user_id and quoted_sender_id and quoted_sender_id == bot_user_id:
        return True
    return False


def _strip_mention(content: str, bot_name: str) -> str:
    """Remove leading @mention of the bot from message content."""
    if not bot_name:
        return content
    import re
    # Remove @BotName at start, case-insensitive
    cleaned = re.sub(rf"^@?{re.escape(bot_name)}\s*[,:]?\s*", "", content, flags=re.IGNORECASE)
    return cleaned.strip() or content


# ── Group filter (custom): cheap rules that keep group noise away from the LLM ──────────────────
# Small-talk rules apply to unmentioned messages in ambient mode, and stand down while the bot is
# talking in the group. Repeat and flood caps apply to every group message, more leniently when the
# bot is mentioned. The rules are deliberately conservative: a message the LLM answers __SILENT__ to
# costs a turn, a skipped request costs trust. docs/log_fix_codebase/zalo_personal_channel_addition.md §8
GROUP_FILTER_BOT_RECENT_S = 120.0   # the bot spoke in the group this recently: small-talk rules stand down
GROUP_FILTER_LONG_CHARS = 25        # from this length on a message is worth the LLM's look
GROUP_FILTER_MAX_SHORT_WORDS = 2    # up to this many words without a question is small talk
GROUP_FILTER_REPEAT = {False: (600.0, 1), True: (300.0, 2)}  # mentioned -> (window s, earlier copies allowed)
GROUP_FILTER_FLOOD = {False: (30.0, 6), True: (60.0, 10)}    # mentioned -> (window s, earlier messages allowed)
GROUP_FILTER_MEMORY = 80            # recent messages remembered per group

# Lists are in "plain" form: lowercase, accents and đ removed (see _plain), so "cảm ơn" and "cam on" match
_ASK_WORDS = (
    "khong", "sao", "gi", "nao", "dau", "bao gio", "may gio", "the nao", "lam sao", "giup", "nho", "ho tro",
    "ai biet", "co ai", "huong dan", "cach", "hoi", "chua", "can", "muon", "xin",
    "how", "why", "what", "where", "when", "which", "who", "help", "please", "pls", "anyone", "could", "would",
)
_ASK = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in _ASK_WORDS) + r")\b")
_ACK_WORDS = {
    "ok", "oke", "okie", "okay", "okk", "oki", "u", "um", "uh", "uhm", "ua", "uk", "ukm", "o", "oh", "a", "ah",
    "da", "vang", "roi", "dc", "duoc", "chuan", "dung", "dung roi", "chuan roi", "cam on", "cam on nhieu",
    "thanks", "thank you", "thank", "tks", "thx", "ty", "done", "xong", "xong roi", "bye", "bb", "chao",
    "hello", "hi", "alo", "yes", "yep", "yeah", "no", "nope", "k", "kk", "ko", "hong", "nice", "good",
    "great", "cool", "wow", "oi", "troi", "troi oi", "hay", "tuyet", "tuyet voi", "dep", "ngon", "vui",
    "buon", "chuc mung", "gg", "lol", "ca nha", "moi nguoi",
}
_PARTICLES = {"a", "nhe", "nha", "nhen", "nghen", "anh", "chi", "em", "ban", "oi", "nhi", "ha", "he", "luon",
              "ne", "do", "day", "qua", "lam", "nhieu", "cac", "moi", "nguoi", "ca", "voi", "ak"}
_LAUGHTER = re.compile(r"(?:ha|hi|he|hu|hm+|lol|lmao|k+|xd|z+)+")
_KEYBOARD_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
_CODE_CHARS = re.compile(r"[\d/\\:@#=\[\]{}<>`|~$%^&*+]")
_LINK = re.compile(r"https?://|www\.", re.IGNORECASE)


def _plain(text: str) -> str:
    """Lowercase, accents and đ removed, only letters, digits and single spaces: the form the lists use."""
    text = unicodedata.normalize("NFD", text.lower()).replace("đ", "d")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"[^\w\s]|_", " ", text)
    return " ".join(text.split())


def _looks_like_a_word(token: str) -> bool:
    """Whether a plain token could be a word rather than keyboard mashing (custom). Short tokens and
    non-Latin scripts always pass; Vietnamese syllables are short and always have a vowel."""
    if len(token) <= 3 or not token.isascii():
        return True
    if re.search(r"(.)\1{3}", token):  # "aaaa", "hhhh"
        return False
    if any(row[i:i + 5] in token for row in _KEYBOARD_ROWS for i in range(len(row) - 4)):  # "asdfg"
        return False
    if sum(ch in "aeiouy" for ch in token) < 0.15 * len(token):
        return False
    return re.search(r"[^aeiouy]{5}", token) is None


def _is_acknowledgement(words: list[str]) -> bool:
    """"ok", "cảm ơn nhiều ạ", "dạ vâng anh": an answer to someone, with its trailing particles dropped."""
    words = list(words)
    while len(words) > 1 and words[-1] in _PARTICLES:
        words.pop()
    return " ".join(words) in _ACK_WORDS


def _group_chatter_reason(content: str, has_media: bool) -> str | None:
    """Why an unmentioned group message is small talk the LLM need not see (custom), else None.

    Anything that could be a request goes through: a question mark or an asking word, a link, numbers
    or code (an error pasted in), a long message. Skipped: media without words, emoji only, laughter,
    an acknowledgement, keyboard mashing, and one or two words that ask nothing.
    """
    text = (content or "").strip()
    if not text:
        return "media without words" if has_media else "empty"
    plain = _plain(text)
    if not plain:
        return "emoji only"
    if "?" in text or _ASK.search(plain):
        return None
    squeezed = plain.replace(" ", "")
    if _LAUGHTER.fullmatch(squeezed):
        return "laughter"
    if _LINK.search(text) or _CODE_CHARS.search(text):
        return None
    words = plain.split()
    if len(squeezed) >= 6 and not any(_looks_like_a_word(w) for w in words):
        return "no real word"
    if not plain.isascii():  # a script the lists do not cover (Chinese, Japanese, Korean, Thai...)
        return None
    if len(text) >= GROUP_FILTER_LONG_CHARS:
        return None
    if _is_acknowledgement(words):
        return "acknowledgement"
    if len(words) <= GROUP_FILTER_MAX_SHORT_WORDS:
        return "short small talk"
    return None


class ZaloPersonalConfig(Base):
    """Zalo personal channel configuration (unofficial, uses zca-js Node.js bridge)."""

    enabled: bool = False
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Zalo user IDs (* = all, behind QR auth)
    node_path: str = "node"  # Path to Node.js binary
    group_reply_mode: Literal["mention", "ambient", "open"] = "ambient"
    # Group filter (custom): "on" keeps small talk, gibberish, repeats and floods away from the LLM;
    # "log" only logs what it would skip, and flags a would-be skip the bot answered; "off" does nothing
    group_filter: Literal["off", "log", "on"] = "log"


class ZaloPersonalChannel(BaseChannel):
    """
    Zalo personal channel using zca-js Node.js bridge.

    Spawns a Node.js subprocess that communicates via stdin/stdout JSON lines.
    QR code is generated on-demand via the Web Dashboard /zalo/setup page.

    ⚠️ This uses an unofficial API — account may be locked/banned.
    """

    name = "zalo"
    display_name = "Zalo"

    # Connection states
    STATUS_DISCONNECTED = "disconnected"
    STATUS_CONNECTING = "connecting"      # Bridge spawned, waiting for QR scan
    STATUS_CONNECTED = "connected"         # Logged in, listening for messages
    STATUS_AUTH_REQUIRED = "auth_required" # Session expired — dashboard must trigger QR re-scan

    # Account opened in Zalo Web/PC elsewhere: yield, and only take it back after
    # this long — reconnecting at once would kick the other session (ping-pong).
    # Doubles while it keeps happening (10, 20, 40, 60 min); resets after the bot
    # stayed connected for DUPLICATE_STREAK_RESET_S.
    DUPLICATE_RECONNECT_DELAY_S = 600.0
    DUPLICATE_RECONNECT_MAX_S = 3600.0
    DUPLICATE_STREAK_RESET_S = 1800.0
    # Bridge delivery acks. On timeout the command already reached the bridge, so
    # it is not retried (a retry would most likely send it twice).
    TEXT_ACK_TIMEOUT_S = 20.0
    MEDIA_ACK_TIMEOUT_S = 180.0  # uploads of large videos/files take time
    OUTBOUND_QUEUE_MAX = 50
    OUTBOUND_QUEUE_TTL_S = 15 * 60.0
    OUTBOUND_FLUSH_SPACING_S = 2.1  # stays under the 5 msgs / 10s per-chat rate limit

    def __init__(self, config: Any, bus: MessageBus):
        super().__init__(config, bus)
        # Parse dict → typed config so BaseChannel.is_allowed() works correctly
        if isinstance(config, dict):
            config = ZaloPersonalConfig.model_validate(config)
            self.config = config  # override self.config set by super().__init__
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._connect_watchdog_task: asyncio.Task | None = None
        self._bridge_dir: Path | None = None
        self._node_path: str = getattr(config, "node_path", "node")
        self._ready_event = asyncio.Event()
        self._status: str = self.STATUS_DISCONNECTED
        self._user_id: str = ""
        self._user_name: str = ""
        self._reconnect_attempts: int = 0
        # Auto-reconnect never gives up (transient failures only) — this counter
        # only drives the exponential backoff delay, capped at 5 min. Reset to 0
        # on a successful "ready" event.
        self._intentional_disconnect: bool = False  # set True when user explicitly disconnects
        # Watchdog: how long a connect may stay in CONNECTING without a QR before
        # we treat it as stuck. Must outlast the bridge's longest saved-login
        # retry sequence (~2 min when the server keeps refusing the session).
        self._connect_timeout_s: float = 240.0
        # Bumped by disconnect(): a connect() suspended in an await gives up
        # instead of bringing the bridge up behind the user's back.
        self._connect_generation: int = 0
        self._pending_commands: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Rate limit: track send timestamps per chat_id to prevent runaway loops
        self._send_timestamps: dict[str, list[float]] = {}
        self._max_sends_per_window: int = 5     # max messages per chat per window (raised from 3 to support multi-message splits)
        self._send_window_seconds: float = 10.0 # sliding window size (reduced from 30s to reset faster between turns)
        # A chat that got this many messages in the last minute is a send loop: refused, not waited for
        self._send_loop_per_minute: int = 3 * self._max_sends_per_window
        self._send_locks: dict[str, asyncio.Lock] = {}  # one chat's sends take their slots in order
        # Registry: remember thread_type per chat_id from inbound messages.
        # Used as fallback when outbound metadata lacks thread_type (e.g. cross-chat message tool).
        self._known_thread_types: dict[str, str] = {}
        # Group filter (custom): recent (time, sender, plain text) per group for the repeat and flood
        # caps, and when the bot last spoke in each group, which pauses the small-talk rules
        self._group_recent: dict[str, deque[tuple[float, str, str]]] = {}
        self._group_bot_spoke: dict[str, float] = {}
        # Tracks which chat_ids have already received the one-per-turn processing hint.
        # Cleared when a real (non-progress) message is sent to that chat.
        self._hint_sent: set[str] = set()
        # The single pending auto-reconnect; an explicit Connect/Disconnect cancels
        # it while it is still waiting out its backoff delay.
        self._reconnect_task: asyncio.Task | None = None
        self._reconnect_waiting: bool = False
        self._last_disconnect_reason: str = ""
        self._duplicate_streak: int = 0
        self._ready_at: float = 0.0  # monotonic time of the last "ready"
        # Messages sent while the bridge is coming back on its own; delivered
        # after the next "ready" (bounded, and dropped once too old).
        self._pending_outbound: list[tuple[float, OutboundMessage]] = []
        self._flush_task: asyncio.Task | None = None

    # ── BaseChannel interface ─────────────────────────────

    async def start(self) -> None:
        """Start the channel — setup bridge files, then auto-login if a saved session exists.

        Without a saved session, connection (QR scan) is triggered from the Web
        Dashboard /zalo/setup page. A user who pressed Disconnect stays
        disconnected across restarts until they press Connect again.
        """
        self._running = True

        # Setup bridge (copy files, npm install) so it's ready when user clicks Connect
        try:
            await self._setup_bridge()
        except Exception as e:
            logger.error("Failed to setup Zalo bridge: {}", e)
            # Stay running: Connect re-runs the setup (e.g. once npm can reach the
            # network after a reboot), and a saved session retries it on its own.
            if self._has_saved_session() and not self._is_manually_disconnected():
                self._schedule_reconnect()
            return

        if not self._has_saved_session():
            logger.info("Zalo channel ready — use Web Dashboard /zalo/setup to connect")
        elif self._is_manually_disconnected():
            logger.info("Zalo saved session found, but user disconnected it — not auto-connecting")
        else:
            logger.info("Zalo saved session found — auto-connecting")
            result = await self.connect()
            if result.get("status") == "error":
                logger.error("Zalo auto-connect failed: {}", result.get("message"))

    async def stop(self) -> None:
        """Stop the channel and disconnect if connected."""
        self._running = False
        # Shutdown is not a user choice — keep auto-connect for the next start.
        await self.disconnect(user_initiated=False)
        logger.info("Zalo channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Zalo."""
        if (
            self._status != self.STATUS_CONNECTED
            or not self._process
            or self._process.returncode is not None
        ):
            self._queue_outbound(msg)
            return

        # While queued messages are being delivered, line up behind them so a
        # new message never overtakes older ones.
        flushing = self._flush_task is not None and not self._flush_task.done()
        if flushing and asyncio.current_task() is not self._flush_task and not msg.metadata.get("_progress"):
            self._pending_outbound.append((time.monotonic(), msg))
            return

        # Progress messages: send one "processing" hint per chat per turn, drop the rest
        if msg.metadata.get("_progress", False):
            if msg.metadata.get("_tool_hint") and msg.chat_id not in self._hint_sent:
                self._hint_sent.add(msg.chat_id)
                hint_thread_type = (
                    msg.metadata.get("thread_type")
                    or self._known_thread_types.get(msg.chat_id, "User")
                )
                try:
                    await self._send_command({
                        "action": "send",
                        "threadId": msg.chat_id,
                        "threadType": hint_thread_type,
                        "content": "⏳ Đang xử lý, xin đợi...",
                    })
                    logger.debug("[Zalo] Sent processing hint to {}", msg.chat_id)
                except Exception as e:
                    logger.debug("[Zalo] Failed to send processing hint: {}", e)
            return

        if not msg.content or msg.content == "[empty message]":
            if not msg.media:
                return
            # Has media but no text — skip text send, continue to send media only

        # LLM chose to stay silent in group ambient mode
        if msg.content and msg.content.strip() == "__SILENT__":
            logger.debug("[Zalo] Group AMBIENT mode — LLM chose not to respond")
            return

        # ── Rate limiting: prevent runaway send loops ──────────────────
        chat_id = msg.chat_id
        await self._take_send_slot(chat_id)
        now = time.monotonic()

        thread_type = msg.metadata.get("thread_type") or self._known_thread_types.get(chat_id, "User")
        if not msg.metadata.get("thread_type") and thread_type != "User":
            logger.debug("[Zalo] thread_type resolved from registry for {}: {}", chat_id, thread_type)
        if thread_type == "Group":
            # The bot is talking in this group: the small-talk rules stand down for a while (custom)
            self._group_bot_spoke[chat_id] = now
            if msg.metadata.get("group_filter"):  # "log": a would-be skip the bot answered is a miss
                logger.warning("[Zalo] Group filter would have skipped a message the bot answered ({}): {}",
                               msg.metadata["group_filter"], msg.metadata.get("group_filter_preview", ""))

        # Send text message (if any)
        if msg.content and msg.content not in ("", "[empty message]"):
            try:
                await self._send_command({
                    "action": "send",
                    "threadId": msg.chat_id,
                    "threadType": thread_type,
                    "content": msg.content,
                }, wait_for_ack=True, timeout_s=self.TEXT_ACK_TIMEOUT_S)
            except TimeoutError:
                logger.error("[Zalo] No delivery ack within {}s for {} — not retrying",
                             self.TEXT_ACK_TIMEOUT_S, chat_id)

        # ── Send media files if any ──
        if msg.media:
            for media_path in msg.media:
                media_type = self._detect_media_type(media_path)
                try:
                    await self._send_command({
                        "action":     "send_media",
                        "threadId":   msg.chat_id,
                        "threadType": thread_type,
                        "content":    "",
                        "filePath":   str(Path(media_path).resolve()),
                        "mediaType":  media_type,
                    }, wait_for_ack=True, timeout_s=self.MEDIA_ACK_TIMEOUT_S)
                    logger.info("[Zalo] Queued media send: {} ({})", Path(media_path).name, media_type)
                except TimeoutError:
                    logger.error("[Zalo] No delivery ack within {}s for {} — not retrying (upload may still finish)",
                                 self.MEDIA_ACK_TIMEOUT_S, Path(media_path).name)
                except Exception as e:
                    logger.warning("[Zalo] Failed to queue media {}: {}", media_path, e)
                    raise

        # Real message sent — reset hint tracker so next turn can send a new hint
        self._hint_sent.discard(chat_id)

    async def _take_send_slot(self, chat_id: str) -> None:
        """Count one send to chat_id under the rate limit, waiting for a slot when the window is full
        (custom). Failing instead let the manager's retries (1, 2, 4 s) run out before a 10 s window
        freed up, so a burst at the wrong moment was lost for good. A send loop is still refused: a
        chat that already got _send_loop_per_minute messages in the last minute raises as before."""
        async with self._send_locks.setdefault(chat_id, asyncio.Lock()):
            while True:
                now = time.monotonic()
                recent = [t for t in self._send_timestamps.get(chat_id, []) if t > now - 60.0]
                self._send_timestamps[chat_id] = recent
                in_window = [t for t in recent if t > now - self._send_window_seconds]
                if len(in_window) < self._max_sends_per_window:
                    recent.append(now)
                    return
                if len(recent) >= self._send_loop_per_minute:
                    logger.warning("Zalo rate limit: {} msgs to {} in 60s, a send loop? Dropping message",
                                   len(recent), chat_id)
                    raise RuntimeError(f"Zalo rate limit: {len(recent)} messages in 60s for {chat_id}")
                wait = in_window[0] + self._send_window_seconds - now
                logger.info("[Zalo] Rate limit: {} msgs to {} in {}s, sending in {:.1f}s",
                            len(in_window), chat_id, self._send_window_seconds, wait)
                await asyncio.sleep(wait + 0.05)  # the margin keeps the oldest send clear of the window edge

    def _group_filter_reason(self, thread_id: str, sender_id: str, content: str, has_media: bool,
                             mentioned: bool) -> str | None:
        """Why a group message should not reach the LLM (custom), else None.

        Repeats and floods are checked for every message, with the lenient caps when the bot is
        mentioned. Small talk is checked only for unmentioned messages, and not while the bot is talking
        in the group (its last message there is under GROUP_FILTER_BOT_RECENT_S old): a short reply to
        the bot ("chưa được") carries no mention.
        """
        now = time.monotonic()
        recent = self._group_recent.setdefault(thread_id, deque(maxlen=GROUP_FILTER_MEMORY))
        plain = _plain(content or "")
        repeat_window, copies_allowed = GROUP_FILTER_REPEAT[mentioned]
        flood_window, burst_allowed = GROUP_FILTER_FLOOD[mentioned]
        copies = sum(1 for t, _, p in recent if p == plain and now - t <= repeat_window) if plain else 0
        burst = sum(1 for t, s, _ in recent if s == sender_id and now - t <= flood_window)
        recent.append((now, sender_id, plain))
        if copies >= copies_allowed:
            return f"repeated {copies + 1} times in {int(repeat_window)} s"
        if burst >= burst_allowed:
            return f"{burst + 1} messages from one sender in {int(flood_window)} s"
        if mentioned or now - self._group_bot_spoke.get(thread_id, float("-inf")) <= GROUP_FILTER_BOT_RECENT_S:
            return None
        return _group_chatter_reason(content, has_media)

    @staticmethod
    def _detect_media_type(path: str) -> str:
        """Detect media type from file extension for Zalo bridge."""
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in ("jpg", "jpeg", "png", "webp"):
            return "photo"
        if ext == "gif":
            return "gif"
        if ext in ("mp4", "mov", "avi", "mkv"):
            return "video"
        if ext in ("ogg", "mp3", "m4a", "aac", "opus"):
            return "voice"
        return "file"

    # ── Public methods for Web Dashboard ──────────────────

    async def connect(self) -> dict:
        """Start the bridge and initiate QR login.

        Returns:
            dict with {"status": "connecting"}, {"status": "error", "message": "..."},
            or {"status": "disconnected"} when a Disconnect/stop arrived meanwhile.
        """
        if self._status == self.STATUS_CONNECTED:
            return {"status": "already_connected", "userId": self._user_id}

        if self._status == self.STATUS_CONNECTING:
            return {"status": "already_connecting"}

        # An explicit Connect wins over a pending auto-reconnect, which would
        # otherwise wake up later and kill the bridge started here.
        self._cancel_reconnect()
        self._intentional_disconnect = False
        self._status = self.STATUS_CONNECTING
        self._ready_event.clear()
        # A Disconnect/stop arriving while this connect() waits below (npm install
        # can take minutes) bumps the generation; then this connect() gives up.
        generation = self._connect_generation
        superseded = {"status": "disconnected"}

        # Never run two bridges side by side: the previous one may still be alive
        # (e.g. in AUTH_REQUIRED, or still waiting for a QR scan).
        if self._process is not None:
            await self._kill_process(graceful=False)
            if generation != self._connect_generation:
                return superseded

        # Ensure bridge is set up (also re-runs a setup whose npm install failed)
        if not self._bridge_dir or not self._bridge_installed():
            try:
                await self._setup_bridge()
            except Exception as e:
                if generation == self._connect_generation:
                    self._status = self.STATUS_DISCONNECTED
                return {"status": "error", "message": str(e)}
            if generation != self._connect_generation:
                return superseded

        # Connecting overrides an earlier "stay disconnected" choice. A QR image
        # left by an earlier attempt is dead: it must not be shown as scannable,
        # nor make the connect watchdog wait for a human scan.
        self._set_manual_disconnect(False)
        self._clear_qr()

        # Spawn Node.js bridge process
        try:
            bridge_js = self._bridge_dir / "zalo_bridge.js"
            proc = await asyncio.create_subprocess_exec(
                self._node_path, str(bridge_js),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._bridge_dir),
            )
            if generation != self._connect_generation:
                proc.kill()  # disconnected/stopped while it was being spawned
                await proc.wait()
                return superseded
            self._process = proc
            logger.info("Zalo bridge process started (PID: {})", proc.pid)
        except FileNotFoundError:
            self._status = self.STATUS_DISCONNECTED
            msg = (
                f"Node.js not found at '{self._node_path}'. "
                "Install Node.js >= 18 or set channels.zalo.nodePath in config."
            )
            logger.error(msg)
            return {"status": "error", "message": msg}
        except Exception as e:
            self._status = self.STATUS_DISCONNECTED
            return {"status": "error", "message": str(e)}

        # Start reading bridge output
        self._reader_task = asyncio.create_task(self._read_bridge_output())
        self._stderr_task = asyncio.create_task(self._read_bridge_stderr())

        # Start watchdog: if auto-login (saved session) hangs in CONNECTING
        # without ever emitting "ready" or "qr", force a reconnect.
        self._connect_watchdog_task = asyncio.create_task(self._connect_watchdog())

        return {"status": "connecting"}

    async def _connect_watchdog(self) -> None:
        """Guard against a connect() that hangs forever in CONNECTING.

        The bridge always ends its saved-session login with "ready",
        "auth_expired" or "max_reconnect_reached", but a network call inside
        zca-js can still hang. After _connect_timeout_s without a QR shown the
        bridge is killed; with a saved session a reconnect is scheduled,
        otherwise (QR flow nobody is watching) the channel just goes back to
        DISCONNECTED. No action if the connection succeeds, a QR is shown
        (waiting for a human scan is legitimate), or status leaves CONNECTING.
        """
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=self._connect_timeout_s)
            return  # "ready" arrived → connected successfully
        except asyncio.TimeoutError:
            pass

        # Timed out. If we are still stuck in CONNECTING (no QR shown, not
        # stopped, not an intentional disconnect), the bridge is wedged.
        if (
            self._running
            and not self._intentional_disconnect
            and self._status == self.STATUS_CONNECTING
        ):
            qr_shown = bool(self._bridge_dir and (self._bridge_dir / "qr.png").exists())
            if qr_shown:
                return  # Waiting for a human QR scan — not stuck
            self._status = self.STATUS_DISCONNECTED
            await self._kill_process(graceful=False)
            if self._has_saved_session():
                logger.warning("Zalo connect watchdog: stuck in CONNECTING for {}s — forcing reconnect",
                               self._connect_timeout_s)
                self._schedule_reconnect()
            else:
                # Retrying a QR login nobody asked for would loop unattended.
                logger.warning("Zalo connect watchdog: no QR after {}s — press Connect to retry",
                               self._connect_timeout_s)

    async def _kill_process(self, *, graceful: bool = True) -> None:
        """Kill the bridge process and clean up reader tasks + state.

        Always runs the full cleanup regardless of current status, so it is
        safe to call from _auto_reconnect() where status is already
        STATUS_DISCONNECTED (which would make disconnect() return early).

        Args:
            graceful: If True, send a "stop" command and wait up to 5s for the
                bridge to exit before killing. If False (e.g. the process is
                already dead during auto-reconnect), kill immediately.
        """
        # Cancel reader tasks and the connect watchdog FIRST, so they never react
        # to an exit we cause ourselves (e.g. take connect() replacing the bridge
        # for a crash). Skip the watchdog if it is the task currently running this
        # cleanup (it called us itself) — a task cannot meaningfully await its own
        # cancellation.
        current = asyncio.current_task()
        tasks_to_cancel = [self._reader_task, self._stderr_task]
        if self._connect_watchdog_task is not current:
            tasks_to_cancel.append(self._connect_watchdog_task)
        if self._flush_task is not current:
            tasks_to_cancel.append(self._flush_task)  # what it did not send stays queued
        for task in tasks_to_cancel:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Send stop command to bridge (graceful shutdown)
        if graceful and self._process and self._process.stdin and self._process.returncode is None:
            try:
                await self._send_command({"action": "stop"})
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    logger.warning("Zalo bridge did not exit gracefully, killing...")
                    self._process.kill()
            except Exception as e:
                logger.debug("Error stopping Zalo bridge: {}", e)
                if self._process and self._process.returncode is None:
                    self._process.kill()
        elif self._process and self._process.returncode is None:
            # Non-graceful (or no stdin): kill outright, and wait so the old
            # bridge is really gone before a new one may be spawned.
            try:
                self._process.kill()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except Exception as e:
                logger.debug("Error killing Zalo bridge: {}", e)

        self._process = None
        self._reader_task = None
        self._stderr_task = None
        if self._connect_watchdog_task is not current:
            self._connect_watchdog_task = None
        self._ready_event.clear()
        self._user_id = ""
        self._user_name = ""
        for future in self._pending_commands.values():
            if not future.done():
                future.set_exception(RuntimeError("Zalo bridge disconnected before delivery ack"))
        self._pending_commands.clear()

    async def disconnect(self, *, user_initiated: bool = True) -> dict:
        """Disconnect from Zalo and kill bridge process.

        A user-initiated disconnect (dashboard) is remembered on disk so start()
        does not auto-connect after a restart; stop() passes user_initiated=False.
        """
        # Disconnect also means "stop the pending auto-reconnect", even though the
        # status already reads DISCONNECTED while it waits out its backoff.
        reconnect_pending = self._cancel_reconnect()
        if self._status == self.STATUS_DISCONNECTED and not reconnect_pending:
            return {"status": "already_disconnected"}
        self._intentional_disconnect = True
        self._connect_generation += 1  # a connect() still in progress must not finish
        if user_initiated:
            self._set_manual_disconnect(True)
        self._pending_outbound.clear()

        await self._kill_process(graceful=True)
        self._status = self.STATUS_DISCONNECTED

        logger.info("Zalo disconnected")
        return {"status": "disconnected"}

    def _schedule_reconnect(self, delay: float | None = None) -> None:
        """Start the auto-reconnect loop, unless one is already pending."""
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_waiting = True  # a task that has not started yet is waiting too
        self._reconnect_task = asyncio.create_task(self._auto_reconnect(delay))

    def _reconnect_is_waiting(self) -> bool:
        task = self._reconnect_task
        return bool(task and not task.done() and self._reconnect_waiting)

    def _cancel_reconnect(self) -> bool:
        """Cancel an auto-reconnect that is still waiting out its delay.

        Returns True if one was cancelled. Once it is past the delay it is inside
        connect() (status CONNECTING) and is left alone — cancelling there could
        orphan a half-spawned bridge.
        """
        task = self._reconnect_task
        if not self._reconnect_is_waiting() or task is asyncio.current_task():
            return False
        task.cancel()
        self._reconnect_task = None
        self._reconnect_waiting = False
        return True

    async def _auto_reconnect(self, delay: float | None = None) -> None:
        """Automatically reconnect after an unexpected disconnection.

        Only ever used for transient failures (network drop, listener close,
        the JS bridge's "max_reconnect_reached", or the account opened in Zalo
        Web elsewhere — then with a long first `delay`). A genuinely expired
        session ("auth_expired") is handled separately and does NOT reach here.

        Because the cause is always transient, we never give up: the bot must
        recover on its own when connectivity returns (e.g. a VPS whose network
        is down for hours overnight). Uses exponential backoff capped at 5 min,
        so once backed off we retry every 5 min indefinitely. The attempt
        counter is reset to 0 on a successful "ready" event.
        """
        while self._running and not self._intentional_disconnect:
            if delay is None:
                self._reconnect_attempts += 1
                delay = min(5 * (2 ** (self._reconnect_attempts - 1)), 300)  # 5s, 10s, 20s… cap 5 min
                # Stop growing the counter once we've hit the cap to avoid unbounded
                # exponentiation; the delay stays pinned at 300s from here on.
                if delay >= 300:
                    self._reconnect_attempts = min(self._reconnect_attempts, 7)  # 5*2^6 = 320 → already capped
            logger.info(
                "Zalo auto-reconnect in {}s (attempt {}, retrying until connected)",
                delay, self._reconnect_attempts,
            )
            self._reconnect_waiting = True
            try:
                await asyncio.sleep(delay)
            finally:
                self._reconnect_waiting = False

            if not self._running or self._intentional_disconnect:
                return  # Channel stopped or user disconnected while waiting

            # connect() replaces any leftover bridge process before spawning.
            result = await self.connect()
            if result.get("status") == "disconnected":
                return  # user disconnected (or channel stopped) while it was connecting
            if result.get("status") in ("connecting", "already_connected", "already_connecting"):
                logger.info("Zalo auto-reconnect: bridge restarted ({})", result.get("status"))
                return
            logger.warning("Zalo auto-reconnect: connect() returned {}", result)
            delay = None

    def get_status(self) -> dict:
        """Get current connection status."""
        result = {
            "status": self._status,
            "userId": self._user_id,
            "userName": self._user_name,
            # Waiting out a backoff before reconnecting on its own (status still
            # reads "disconnected"); the dashboard shows it as RECONNECTING.
            "reconnecting": self._reconnect_is_waiting(),
            "disconnectReason": self._last_disconnect_reason,
        }

        # Check if QR image exists
        if self._bridge_dir:
            qr_path = self._bridge_dir / "qr.png"
            result["hasQr"] = qr_path.exists()
            # Check if saved credentials exist
            creds_path = self._bridge_dir / "credentials.json"
            result["hasSavedSession"] = creds_path.exists()
            # Zalo refused the saved session (it was set aside); survives restarts,
            # unlike the AUTH_REQUIRED status. Cleared by the next QR login.
            result["sessionExpired"] = (
                not result["hasSavedSession"]
                and (self._bridge_dir / "credentials.rejected.json").exists()
            )
        else:
            result["hasQr"] = False
            result["hasSavedSession"] = False
            result["sessionExpired"] = False

        return result

    def get_qr_path(self) -> Path | None:
        """Get path to QR code image file, or None if not available."""
        if self._bridge_dir:
            qr_path = self._bridge_dir / "qr.png"
            if qr_path.exists():
                return qr_path
        return None

    # ── Internal methods ──────────────────────────────────

    def _has_saved_session(self) -> bool:
        """True if the bridge has saved login credentials (auto-login, no QR)."""
        return bool(self._bridge_dir and (self._bridge_dir / "credentials.json").exists())

    def _is_manually_disconnected(self) -> bool:
        """True if the user pressed Disconnect, so start() must not auto-connect."""
        return bool(self._bridge_dir and (self._bridge_dir / _MANUAL_DISCONNECT_FLAG).exists())

    def _set_manual_disconnect(self, value: bool) -> None:
        if not self._bridge_dir:
            return
        flag = self._bridge_dir / _MANUAL_DISCONNECT_FLAG
        try:
            if value:
                flag.touch()
            else:
                flag.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Could not update Zalo manual-disconnect flag: {}", e)

    def _clear_qr(self) -> None:
        """Delete a QR image that no running bridge is waiting on."""
        if self._bridge_dir:
            (self._bridge_dir / "qr.png").unlink(missing_ok=True)

    def _bridge_installed(self) -> bool:
        """True if zca-js is really installed (an interrupted npm install leaves node_modules without it)."""
        return bool(self._bridge_dir and (self._bridge_dir / "node_modules" / "zca-js" / "package.json").exists())

    def _queue_outbound(self, msg: OutboundMessage) -> None:
        """Hold a message while the bridge is coming back on its own; drop it otherwise."""
        # CONNECTED here means the bridge just died and the reader has not seen
        # the EOF yet; it schedules the reconnect a moment later.
        coming_back = self._status in (self.STATUS_CONNECTING, self.STATUS_CONNECTED) or (
            self._reconnect_task is not None and not self._reconnect_task.done()
        )
        if msg.metadata.get("_progress"):
            return  # transient progress hint, not worth delivering late
        if not coming_back or self._intentional_disconnect:
            logger.warning("Zalo not connected ({}) — dropping message to {}", self._status, msg.chat_id)
            return
        now = time.monotonic()
        self._pending_outbound = [
            (t, m) for t, m in self._pending_outbound if now - t < self.OUTBOUND_QUEUE_TTL_S
        ]
        if len(self._pending_outbound) >= self.OUTBOUND_QUEUE_MAX:
            _, oldest = self._pending_outbound.pop(0)
            logger.warning("Zalo outbound queue full — dropping oldest message to {}", oldest.chat_id)
        self._pending_outbound.append((now, msg))
        logger.warning(
            "Zalo reconnecting — queued message to {} ({} pending)",
            msg.chat_id, len(self._pending_outbound),
        )

    async def _flush_outbound(self) -> None:
        """Deliver messages queued while the bridge was reconnecting, oldest first.

        Takes them off the queue one at a time (send() appends new messages behind
        them meanwhile), so whatever is not sent yet stays queued if the bridge
        drops again or this task is cancelled.
        """
        logger.info("Zalo: delivering {} message(s) queued while reconnecting", len(self._pending_outbound))
        sent_any = False
        expired = 0
        while self._pending_outbound and self._status == self.STATUS_CONNECTED:
            if time.monotonic() - self._pending_outbound[0][0] >= self.OUTBOUND_QUEUE_TTL_S:
                self._pending_outbound.pop(0)
                expired += 1
                continue
            if sent_any:
                await asyncio.sleep(self.OUTBOUND_FLUSH_SPACING_S)
                if not self._pending_outbound or self._status != self.STATUS_CONNECTED:
                    break
            _, msg = self._pending_outbound.pop(0)
            try:
                await self.send(msg)
            except Exception as e:
                logger.warning("Zalo: failed to deliver queued message to {}: {}", msg.chat_id, e)
            sent_any = True
        if expired:
            logger.warning("Zalo: dropped {} queued message(s) older than {}s",
                           expired, int(self.OUTBOUND_QUEUE_TTL_S))

    async def _setup_bridge(self) -> None:
        """Setup the bridge directory with Node.js files and install dependencies."""
        from nanobot.config.paths import get_data_dir

        data_dir = get_data_dir()
        self._bridge_dir = data_dir / "zalo_bridge"
        self._bridge_dir.mkdir(parents=True, exist_ok=True)

        # Copy bridge files
        dest_js = self._bridge_dir / "zalo_bridge.js"
        dest_pkg = self._bridge_dir / "package.json"

        shutil.copy2(_BRIDGE_JS, dest_js)
        shutil.copy2(_BRIDGE_PKG, dest_pkg)
        logger.debug("Copied Zalo bridge files to {}", self._bridge_dir)

        # Install npm dependencies if needed (zca-js missing = never or partly installed)
        if not self._bridge_installed():
            logger.info("Installing Zalo bridge dependencies (first time)...")
            npm_cmd = "npm.cmd" if sys.platform == "win32" else "npm"
            proc = await asyncio.create_subprocess_exec(
                npm_cmd, "install", "--production",
                cwd=str(self._bridge_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                err_msg = stderr.decode(errors="replace")
                raise RuntimeError(f"npm install failed: {err_msg}")
            logger.info("Zalo bridge dependencies installed successfully")

    async def _send_command(self, cmd: dict, *, wait_for_ack: bool = False, timeout_s: float = 20.0) -> dict[str, Any] | None:
        """Send a JSON command to the bridge via stdin."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("Zalo bridge is not running")
        future: asyncio.Future[dict[str, Any]] | None = None
        cmd_id: str | None = None
        try:
            if wait_for_ack:
                cmd_id = uuid.uuid4().hex
                cmd = {**cmd, "cmdId": cmd_id}
                future = asyncio.get_running_loop().create_future()
                self._pending_commands[cmd_id] = future
            line = json.dumps(cmd, ensure_ascii=False) + "\n"
            self._process.stdin.write(line.encode("utf-8"))
            await self._process.stdin.drain()
            if future is not None:
                return await asyncio.wait_for(future, timeout=timeout_s)
            return None
        except TimeoutError:
            # No ack in time. TimeoutError is an OSError since Python 3.11, so it
            # must pass through here instead of becoming a retryable RuntimeError.
            raise
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.warning("Failed to send command to Zalo bridge: {}", e)
            raise RuntimeError(f"Failed to send command to Zalo bridge: {e}") from e
        finally:
            if cmd_id and future is not None:
                self._pending_commands.pop(cmd_id, None)

    async def _read_bridge_output(self) -> None:
        """Read JSON lines from bridge stdout and dispatch events."""
        # Bound to the process it was started for: a stale reader must never read
        # from, or react to the exit of, a bridge spawned later by connect().
        proc = self._process
        if not proc or not proc.stdout:
            return

        eof = False
        cancelled = False
        try:
            # Read until EOF, not until returncode is set: the bridge's last event,
            # written right before it exits (e.g. "auth_expired"), must not be lost.
            while True:
                line_bytes = await proc.stdout.readline()
                if not line_bytes:
                    eof = True
                    break  # EOF — process exited

                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Non-JSON from Zalo bridge: {}", line[:200])
                    continue

                await self._handle_bridge_event(event)

        except asyncio.CancelledError:
            cancelled = True  # _kill_process() stopped us; its caller owns the state
        except Exception as e:
            # E.g. an event handler raised: this reader is gone, so recover through
            # a reconnect below instead of leaving a CONNECTED channel deaf.
            logger.error("Error reading Zalo bridge output: {}", e)
        finally:
            if not cancelled and self._running and not self._intentional_disconnect and proc is self._process:
                if self._status == self.STATUS_CONNECTED:
                    logger.warning("Zalo bridge output stream ended unexpectedly — scheduling reconnect")
                    self._status = self.STATUS_DISCONNECTED
                    self._schedule_reconnect()
                elif eof and self._status == self.STATUS_CONNECTING:
                    # Bridge died before "ready" without a final event (crash or a
                    # failed QR login): never stay stuck in CONNECTING behind a dead QR.
                    self._status = self.STATUS_DISCONNECTED
                    self._clear_qr()
                    if self._has_saved_session():
                        logger.warning("Zalo bridge exited during auto-login — scheduling reconnect")
                        self._schedule_reconnect()
                    else:
                        logger.warning("Zalo bridge exited during QR login — press Connect to retry")

    async def _read_bridge_stderr(self) -> None:
        """Read bridge stderr and forward to loguru (debug logs)."""
        proc = self._process
        if not proc or not proc.stderr:
            return

        try:
            while True:
                line_bytes = await proc.stderr.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if line:
                    logger.debug("[ZaloBridge] {}", line)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _handle_bridge_event(self, event: dict) -> None:
        """Handle an event from the Zalo bridge."""
        event_type = event.get("event", "")

        if event_type == "ready":
            self._user_id = event.get("userId", "unknown")
            self._status = self.STATUS_CONNECTED
            self._reconnect_attempts = 0  # reset backoff counter on successful connect
            self._last_disconnect_reason = ""
            self._ready_at = time.monotonic()
            self._ready_event.set()
            logger.info("Zalo logged in as user {}", self._user_id)

            # Delete QR image after successful login
            if self._bridge_dir:
                qr_path = self._bridge_dir / "qr.png"
                if qr_path.exists():
                    qr_path.unlink(missing_ok=True)

            # Deliver what was queued while reconnecting — in its own task: send()
            # waits for acks that this very reader task has to read.
            if self._pending_outbound and (self._flush_task is None or self._flush_task.done()):
                self._flush_task = asyncio.create_task(self._flush_outbound())

        elif event_type == "qr":
            logger.info("Zalo QR code received — scan with Zalo app")

        elif event_type == "message":
            thread_id = event.get("threadId", "")
            sender_id = event.get("senderId", thread_id)
            content = event.get("content", "")
            thread_type = event.get("threadType", "User")
            sender_name = event.get("senderName", "")
            mentioned_ids = event.get("mentionedIds", [])
            quoted_sender_id = event.get("quotedSenderId")  # None if not a reply
            quoted_content = event.get("quotedContent")     # None if no quoted text
            # ── Media fields ──
            media_url   = event.get("mediaUrl")    # None for text-only messages
            media_thumb = event.get("mediaThumb")  # reserved, not used yet
            media_type  = event.get("mediaType")   # "photo"|"video"|"voice"|"gif"|"file"|None

            if not content and not media_url:
                return

            # ── Group chat filter (Option C) ───────────────────────────
            group_ambient = False
            filter_note = ""
            if thread_type == "Group":
                mentioned = _is_mentioned(
                    content, self._user_id, self._user_name,
                    mentioned_ids, quoted_sender_id,
                )
                if not mentioned and self.config.group_reply_mode == "mention":
                    logger.debug(
                        "[Zalo] Group message from {} ignored by group_reply_mode=mention",
                        sender_name or sender_id,
                    )
                    return
                if not mentioned and self.config.group_reply_mode == "open":
                    # Explicit branch for readability: open mode intentionally treats
                    # unmentioned group messages like normal messages.
                    logger.debug(
                        "[Zalo] Group message from {} - OPEN mode, responding",
                        sender_name or sender_id,
                    )
                    mentioned = True
                if mentioned:
                    # Strip @mention from content before forwarding to agent
                    content = _strip_mention(content, self._user_name)
                    logger.debug("[Zalo] Group message — bot mentioned/replied-to, responding")
                else:
                    # Not mentioned: pass to LLM in AMBIENT mode
                    group_ambient = True
                    logger.debug(
                        "[Zalo] Group message from {} — AMBIENT mode (LLM decides)",
                        sender_name or sender_id,
                    )
                # Group filter (custom): repeats and floods for every group message, small talk only in
                # ambient. "log" lets the message through and notes what would have happened to it.
                filter_mode = getattr(self.config, "group_filter", "off")
                if filter_mode in ("log", "on"):
                    reason = self._group_filter_reason(thread_id, sender_id, content, bool(media_url), mentioned)
                    if reason and filter_mode == "on":
                        logger.info("[Zalo] Group filter skipped {} ({}): {}",
                                    sender_name or sender_id, reason, content[:60])
                        return
                    if reason:
                        logger.info("[Zalo] Group filter would skip {} ({}): {}",
                                    sender_name or sender_id, reason, content[:60])
                        filter_note = reason

            # ── Download media if present (after the group filter: a skipped message downloads nothing) ──
            media_paths: list[str] = []
            if media_url:
                try:
                    import time as _time
                    import httpx
                    from nanobot.config.paths import get_media_dir
                    ext_map = {
                        "photo": ".jpg", "gif": ".gif", "video": ".mp4",
                        "voice": ".ogg", "sticker": ".png", "file": ".bin",
                    }
                    ext = ext_map.get(media_type or "", ".bin")
                    media_dir = get_media_dir("zalo")
                    media_dir.mkdir(parents=True, exist_ok=True)
                    unique_id = f"{thread_id}_{int(_time.time() * 1000)}"
                    file_path = media_dir / f"{unique_id}{ext}"
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        resp = await client.get(media_url)
                        resp.raise_for_status()
                        await asyncio.to_thread(file_path.write_bytes, resp.content)
                    media_paths.append(str(file_path))
                    logger.info("[Zalo] Downloaded {} → {}", media_type, file_path.name)
                except Exception as e:
                    logger.warning("[Zalo] Failed to download media: {}", e)
                    # Continue — still forward any text content if present

            session_key = f"zalo:{thread_id}"

            logger.info(
                "[Zalo] Message from {} ({}{}) : {}",
                sender_name or sender_id,
                thread_type,
                " · AMBIENT" if group_ambient else "",
                content[:80] if content else "[" + (media_type or "media") + " media]",
            )

            # Build ambient note: prepended to content so LLM sees the instruction
            # without modifying the session history in a confusing way.
            deliver_content = content

            # Prepend quoted context so LLM always knows what was replied to
            if quoted_content:
                deliver_content = f"[Quoted: \"{quoted_content}\"]\n\n{deliver_content}"

            if group_ambient:
                # The quote stays here too: it says what the message answers ("9h nhé" to "Mai họp mấy
                # giờ?"), which the bot needs to judge whether to chime in (custom)
                quote = f"[Quoted: \"{quoted_content}\"]\n" if quoted_content else ""
                ambient_note = (
                    f"[GROUP AMBIENT — you were NOT @mentioned. "
                    f"The following is a group message from {sender_name or sender_id}. "
                    "Only respond if it is clearly directed at you or you can genuinely help. "
                    "If the message is just general group chat, output exactly: __SILENT__]\n\n"
                    f"{quote}{sender_name or sender_id}: {content if content else '[sent a media]'}"
                )
                deliver_content = ambient_note

            # Record thread_type so outbound can resolve it for cross-chat sends
            self._known_thread_types[thread_id] = thread_type

            await self._handle_message(
                sender_id=sender_id,
                chat_id=thread_id,
                content=deliver_content,
                media=media_paths if media_paths else None,
                metadata={
                    "thread_type": thread_type,
                    "sender_name": sender_name,
                    "group_ambient": group_ambient,
                    # Group filter in "log": what it would have done, scored in send() against the answer
                    **({"group_filter": filter_note, "group_filter_preview": content[:60]} if filter_note else {}),
                },
                session_key=session_key,
            )

        elif event_type == "disconnected":
            reason = event.get("reason", "unknown")
            logger.warning("Zalo disconnected: {}", reason)
            self._last_disconnect_reason = reason
            if reason == "auth_expired":
                # Session is invalid — user must re-scan QR.
                self._status = self.STATUS_AUTH_REQUIRED
                self._pending_outbound.clear()  # needs a human; don't hold messages
                logger.error(
                    "Zalo requires re-authentication — please scan QR again in dashboard"
                )
            elif reason == "duplicate_connection":
                # The account was opened in Zalo Web/PC elsewhere (one web listener
                # per account). Session still valid: yield, retake it much later,
                # and later still if its owner keeps taking it back.
                self._status = self.STATUS_DISCONNECTED
                if time.monotonic() - self._ready_at >= self.DUPLICATE_STREAK_RESET_S:
                    self._duplicate_streak = 0
                self._duplicate_streak += 1
                delay = min(
                    self.DUPLICATE_RECONNECT_DELAY_S * 2 ** (self._duplicate_streak - 1),
                    self.DUPLICATE_RECONNECT_MAX_S,
                )
                logger.warning(
                    "Zalo account opened elsewhere (Zalo Web/PC) — bot yields, retrying in {}s",
                    int(delay),
                )
                if self._running and not self._intentional_disconnect:
                    self._schedule_reconnect(delay)
            elif reason == "max_reconnect_reached":
                # JS bridge gave up after max retries but session is still valid.
                # Restart the bridge process — no QR needed.
                self._status = self.STATUS_DISCONNECTED
                logger.warning("Zalo JS bridge exhausted retries — restarting bridge process")
                if self._running and not self._intentional_disconnect:
                    self._schedule_reconnect()
            else:
                # Unexpected drop — Python side triggers its own reconnect.
                self._status = self.STATUS_DISCONNECTED
                if self._running and not self._intentional_disconnect:
                    self._schedule_reconnect()

        elif event_type == "error":
            message = event.get("message", "unknown error")
            logger.error("Zalo bridge error: {}", message)

        elif event_type in {"sent", "send_error"}:
            cmd_id = event.get("cmdId")
            if not cmd_id:
                return
            future = self._pending_commands.get(cmd_id)
            if not future or future.done():
                return
            if event_type == "sent":
                future.set_result(event)
            else:
                future.set_exception(RuntimeError(event.get("message", "Unknown Zalo send error")))

        else:
            logger.debug("Unknown Zalo bridge event: {}", event_type)
