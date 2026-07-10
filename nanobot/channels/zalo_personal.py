"""Zalo personal channel — unofficial, uses zca-js (Node.js) bridge."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
import uuid
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


class ZaloPersonalConfig(Base):
    """Zalo personal channel configuration (unofficial, uses zca-js Node.js bridge)."""

    enabled: bool = False
    allow_from: list[str] = Field(default_factory=lambda: ["*"])  # Zalo user IDs (* = all, behind QR auth)
    node_path: str = "node"  # Path to Node.js binary
    group_reply_mode: Literal["mention", "ambient", "open"] = "ambient"


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
        # Watchdog: how long auto-login (saved session) may stay in CONNECTING
        # before we treat it as stuck and force a reconnect. Cancelled if a QR
        # event arrives (waiting for a human scan is legitimate and unbounded).
        self._connect_timeout_s: float = 90.0
        self._pending_commands: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Rate limit: track send timestamps per chat_id to prevent runaway loops
        self._send_timestamps: dict[str, list[float]] = {}
        self._max_sends_per_window: int = 5     # max messages per chat per window (raised from 3 to support multi-message splits)
        self._send_window_seconds: float = 10.0 # sliding window size (reduced from 30s to reset faster between turns)
        # Registry: remember thread_type per chat_id from inbound messages.
        # Used as fallback when outbound metadata lacks thread_type (e.g. cross-chat message tool).
        self._known_thread_types: dict[str, str] = {}
        # Tracks which chat_ids have already received the one-per-turn processing hint.
        # Cleared when a real (non-progress) message is sent to that chat.
        self._hint_sent: set[str] = set()

    # ── BaseChannel interface ─────────────────────────────

    async def start(self) -> None:
        """Start the channel — just setup bridge files, don't connect yet.

        Connection is triggered on-demand from the Web Dashboard /zalo/setup page.
        """
        self._running = True

        # Setup bridge (copy files, npm install) so it's ready when user clicks Connect
        try:
            await self._setup_bridge()
            logger.info("Zalo channel ready — use Web Dashboard /zalo/setup to connect")
        except Exception as e:
            logger.error("Failed to setup Zalo bridge: {}", e)
            self._running = False

    async def stop(self) -> None:
        """Stop the channel and disconnect if connected."""
        self._running = False
        await self.disconnect()
        logger.info("Zalo channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Zalo."""
        if self._status != self.STATUS_CONNECTED:
            logger.warning("Zalo not connected, cannot send")
            return

        if not self._process or self._process.returncode is not None:
            logger.warning("Zalo bridge not running, cannot send")
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
        now = time.monotonic()
        chat_id = msg.chat_id
        timestamps = self._send_timestamps.setdefault(chat_id, [])
        # Purge entries outside the window
        cutoff = now - self._send_window_seconds
        self._send_timestamps[chat_id] = [t for t in timestamps if t > cutoff]
        timestamps = self._send_timestamps[chat_id]

        if len(timestamps) >= self._max_sends_per_window:
            logger.warning(
                "Zalo rate limit: {} msgs to {} in {}s — dropping message",
                len(timestamps), chat_id, self._send_window_seconds,
            )
            raise RuntimeError(
                f"Zalo rate limit: {len(timestamps)} messages in {self._send_window_seconds}s for {chat_id}"
            )
        timestamps.append(now)

        thread_type = msg.metadata.get("thread_type") or self._known_thread_types.get(chat_id, "User")
        if not msg.metadata.get("thread_type") and thread_type != "User":
            logger.debug("[Zalo] thread_type resolved from registry for {}: {}", chat_id, thread_type)

        # Send text message (if any)
        if msg.content and msg.content not in ("", "[empty message]"):
            await self._send_command({
                "action": "send",
                "threadId": msg.chat_id,
                "threadType": thread_type,
                "content": msg.content,
            }, wait_for_ack=True)

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
                    }, wait_for_ack=True)
                    logger.info("[Zalo] Queued media send: {} ({})", Path(media_path).name, media_type)
                except Exception as e:
                    logger.warning("[Zalo] Failed to queue media {}: {}", media_path, e)
                    raise

        # Real message sent — reset hint tracker so next turn can send a new hint
        self._hint_sent.discard(chat_id)

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
            dict with {"status": "connecting"} or {"status": "error", "message": "..."}
        """
        if self._status == self.STATUS_CONNECTED:
            return {"status": "already_connected", "userId": self._user_id}

        if self._status == self.STATUS_CONNECTING:
            return {"status": "already_connecting"}

        self._intentional_disconnect = False
        self._status = self.STATUS_CONNECTING
        self._ready_event.clear()

        # Ensure bridge is set up
        if not self._bridge_dir:
            try:
                await self._setup_bridge()
            except Exception as e:
                self._status = self.STATUS_DISCONNECTED
                return {"status": "error", "message": str(e)}

        # Spawn Node.js bridge process
        try:
            bridge_js = self._bridge_dir / "zalo_bridge.js"
            self._process = await asyncio.create_subprocess_exec(
                self._node_path, str(bridge_js),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._bridge_dir),
            )
            logger.info("Zalo bridge process started (PID: {})", self._process.pid)
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

        The JS bridge can silently get stuck (e.g. saved-session login fails
        with a non-auth/network error and then blocks on loginQR()), leaving
        Python in STATUS_CONNECTING with no "ready" or "disconnected" event.
        This watchdog gives up after _connect_timeout_s and triggers a
        reconnect. It exits early (no action) if the connection succeeds, a QR
        is shown (waiting for a human scan is legitimate), or status leaves
        CONNECTING by any other path.
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
            logger.warning(
                "Zalo connect watchdog: stuck in CONNECTING for {}s — forcing reconnect",
                self._connect_timeout_s,
            )
            self._status = self.STATUS_DISCONNECTED
            await self._kill_process(graceful=False)
            asyncio.create_task(self._auto_reconnect())

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
            # Non-graceful (or no stdin): kill outright
            try:
                self._process.kill()
            except Exception as e:
                logger.debug("Error killing Zalo bridge: {}", e)

        # Cancel reader tasks and the connect watchdog. Skip the watchdog if it
        # is the task currently running this cleanup (it called us itself) — a
        # task cannot meaningfully await its own cancellation.
        current = asyncio.current_task()
        tasks_to_cancel = [self._reader_task, self._stderr_task]
        if self._connect_watchdog_task is not current:
            tasks_to_cancel.append(self._connect_watchdog_task)
        for task in tasks_to_cancel:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

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

    async def disconnect(self) -> dict:
        """Disconnect from Zalo and kill bridge process."""
        if self._status == self.STATUS_DISCONNECTED:
            return {"status": "already_disconnected"}
        self._intentional_disconnect = True

        await self._kill_process(graceful=True)
        self._status = self.STATUS_DISCONNECTED

        logger.info("Zalo disconnected")
        return {"status": "disconnected"}

    async def _auto_reconnect(self) -> None:
        """Attempt to automatically reconnect after an unexpected disconnection.

        Only ever called for transient failures (network drop, listener close,
        or the JS bridge's "max_reconnect_reached"). A genuinely expired session
        ("auth_expired") is handled separately and does NOT reach here.

        Because the cause is always transient, we never give up: the bot must
        recover on its own when connectivity returns (e.g. a VPS whose network
        is down for hours overnight). Uses exponential backoff capped at 5 min,
        so once backed off we retry every 5 min indefinitely. The attempt
        counter is reset to 0 on a successful "ready" event.
        """
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
        await asyncio.sleep(delay)

        if not self._running:
            return  # Channel was stopped while waiting — abort

        # Clean up the dead process before spawning a new one.
        # Use _kill_process() directly (not disconnect()) — disconnect() would
        # return early because status is already STATUS_DISCONNECTED here, leaving
        # the old (zombie) Node.js process alive.
        await self._kill_process(graceful=False)
        result = await self.connect()
        if result.get("status") in ("connecting", "already_connected", "already_connecting"):
            logger.info("Zalo auto-reconnect: bridge restarted ({})", result.get("status"))
        else:
            logger.warning("Zalo auto-reconnect: connect() returned {}", result)
            asyncio.create_task(self._auto_reconnect())

    def get_status(self) -> dict:
        """Get current connection status."""
        result = {
            "status": self._status,
            "userId": self._user_id,
            "userName": self._user_name,
        }

        # Check if QR image exists
        if self._bridge_dir:
            qr_path = self._bridge_dir / "qr.png"
            result["hasQr"] = qr_path.exists()
            # Check if saved credentials exist
            creds_path = self._bridge_dir / "credentials.json"
            result["hasSavedSession"] = creds_path.exists()
        else:
            result["hasQr"] = False
            result["hasSavedSession"] = False

        return result

    def get_qr_path(self) -> Path | None:
        """Get path to QR code image file, or None if not available."""
        if self._bridge_dir:
            qr_path = self._bridge_dir / "qr.png"
            if qr_path.exists():
                return qr_path
        return None

    # ── Internal methods ──────────────────────────────────

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

        # Install npm dependencies if needed
        node_modules = self._bridge_dir / "node_modules"
        if not node_modules.exists():
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
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.warning("Failed to send command to Zalo bridge: {}", e)
            raise RuntimeError(f"Failed to send command to Zalo bridge: {e}") from e
        finally:
            if cmd_id and future is not None:
                self._pending_commands.pop(cmd_id, None)

    async def _read_bridge_output(self) -> None:
        """Read JSON lines from bridge stdout and dispatch events."""
        if not self._process or not self._process.stdout:
            return

        try:
            while self._running and self._process.returncode is None:
                line_bytes = await self._process.stdout.readline()
                if not line_bytes:
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
            pass
        except Exception as e:
            logger.error("Error reading Zalo bridge output: {}", e)
        finally:
            if self._running and self._status == self.STATUS_CONNECTED and not self._intentional_disconnect:
                logger.warning("Zalo bridge output stream ended unexpectedly — scheduling reconnect")
                self._status = self.STATUS_DISCONNECTED
                asyncio.create_task(self._auto_reconnect())

    async def _read_bridge_stderr(self) -> None:
        """Read bridge stderr and forward to loguru (debug logs)."""
        if not self._process or not self._process.stderr:
            return

        try:
            while self._process.returncode is None:
                line_bytes = await self._process.stderr.readline()
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
            self._ready_event.set()
            logger.info("Zalo logged in as user {}", self._user_id)

            # Delete QR image after successful login
            if self._bridge_dir:
                qr_path = self._bridge_dir / "qr.png"
                if qr_path.exists():
                    qr_path.unlink(missing_ok=True)

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

            # ── Download media if present ──
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


            # ── Group chat filter (Option C) ───────────────────────────
            group_ambient = False
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
                ambient_note = (
                    f"[GROUP AMBIENT — you were NOT @mentioned. "
                    f"The following is a group message from {sender_name or sender_id}. "
                    "Only respond if it is clearly directed at you or you can genuinely help. "
                    "If the message is just general group chat, output exactly: __SILENT__]\n\n"
                    f"{sender_name or sender_id}: {content if content else '[sent a media]'}"
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
                },
                session_key=session_key,
            )

        elif event_type == "disconnected":
            reason = event.get("reason", "unknown")
            logger.warning("Zalo disconnected: {}", reason)
            if reason == "auth_expired":
                # Session is invalid — user must re-scan QR.
                self._status = self.STATUS_AUTH_REQUIRED
                logger.error(
                    "Zalo requires re-authentication — please scan QR again in dashboard"
                )
            elif reason == "max_reconnect_reached":
                # JS bridge gave up after max retries but session is still valid.
                # Restart the bridge process — no QR needed.
                self._status = self.STATUS_DISCONNECTED
                logger.warning("Zalo JS bridge exhausted retries — restarting bridge process")
                if self._running and not self._intentional_disconnect:
                    asyncio.create_task(self._auto_reconnect())
            else:
                # Unexpected drop — Python side triggers its own reconnect.
                self._status = self.STATUS_DISCONNECTED
                if self._running and not self._intentional_disconnect:
                    asyncio.create_task(self._auto_reconnect())

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
