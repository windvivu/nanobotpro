"""Message tool for sending messages to users."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Awaitable, Callable

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import ArraySchema, StringSchema, tool_parameters_schema
from nanobot.agent.tools.turn_state import TurnLocal
from nanobot.bus.events import OutboundMessage

# Stop reason of a turn the bot ended by sending its reply with files to the current chat (custom)
ANSWERED_WITH_FILES = "answered_with_files"


@tool_parameters(
    tool_parameters_schema(
        content=StringSchema("The message content to send"),
        channel=StringSchema("Optional: target channel (telegram, discord, etc.)"),
        chat_id=StringSchema("Optional: target chat/user ID"),
        media=ArraySchema(
            StringSchema(""),
            description="Optional: list of file paths to attach (images, audio, documents)",
        ),
        buttons=ArraySchema(
            ArraySchema(StringSchema("Button label")),
            description="Optional: rows of reply buttons to send with the message",
        ),
        required=["content"],
    )
)
class MessageTool(Tool):
    """Tool to send messages to users on chat channels."""

    # One instance serves every session: the target chat and the "sent" flag are per turn (custom)
    _default_channel = TurnLocal()
    _default_chat_id = TurnLocal()
    _default_message_id = TurnLocal()
    _default_metadata = TurnLocal()
    _sent_in_turn = TurnLocal()
    # Files sent to the current chat are the turn's reply: the turn ends there (custom, answer with files)
    _answered_with_files = TurnLocal()
    _answer_content = TurnLocal()

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
        working_dir: str | None = None,
        allowed_dir: Path | None = None,
        extra_allowed_dirs: list[Path] | None = None,
    ):
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._default_message_id = default_message_id
        self._default_metadata: dict[str, Any] = {}
        self._sent_in_turn: bool = False
        self._answered_with_files: bool = False
        self._answer_content: str = ""
        self._working_dir = Path(working_dir) if working_dir else None
        # Where attachments may come from: the bot's file scope (custom, see _resolve_media_paths)
        self._allowed_dir = allowed_dir
        self._extra_allowed_dirs = extra_allowed_dirs

    def set_context(
        self,
        channel: str,
        chat_id: str,
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Set the current message context."""
        self._default_channel = channel
        self._default_chat_id = chat_id
        self._default_message_id = message_id
        self._default_metadata = dict(metadata or {})

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    def start_turn(self) -> None:
        """Reset per-turn send tracking."""
        self._sent_in_turn = False
        self._answered_with_files = False
        self._answer_content = ""

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return (
            "Send a message to a SPECIFIC chat channel or attach files. "
            "WARNING: Do NOT use this tool to reply in normal conversation — just return text directly instead. "
            "Only use this tool when you need to: (1) send files/media via the 'media' parameter, "
            "or (2) send a message to a DIFFERENT chat/channel than the current one. "
            "Never call this tool more than once per turn for the same chat_id. "
            "Sending files to the current chat IS your reply and ends your turn: write your complete "
            "answer in 'content' and send it as the last step, once the files are ready."
        )

    async def execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        media: list[str] | None = None,
        buttons: list[list[str]] | None = None,
        **kwargs: Any
    ) -> str:
        from nanobot.utils.helpers import strip_think
        content = strip_think(content)
        
        channel = channel or self._default_channel
        chat_id = chat_id or self._default_chat_id
        # Only inherit default message_id when targeting the same channel+chat.
        # Cross-chat sends must not carry the original message_id, because
        # some channels (e.g. Feishu) use it to determine the target
        # conversation via their Reply API, which would route the message
        # to the wrong chat entirely.
        if channel == self._default_channel and chat_id == self._default_chat_id:
            message_id = message_id or self._default_message_id
        else:
            message_id = None

        if not channel or not chat_id:
            return "Error: No target channel/chat specified"

        if not self._send_callback:
            return "Error: Message sending not configured"

        try:
            resolved_media = self._resolve_media_paths(media or [])
        except PermissionError as exc:
            return f"Error: {exc}"
        metadata = self._build_metadata(message_id, kwargs)
        if channel == self._default_channel and chat_id == self._default_chat_id:
            if "thread_type" not in metadata and "thread_type" in self._default_metadata:
                metadata["thread_type"] = self._default_metadata["thread_type"]
            # Sent while answering a sub-agent's result: a late reply, which Web Chat pushes (custom)
            if self._default_metadata.get("_late_reply"):
                metadata["_late_reply"] = True
        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            media=resolved_media,
            metadata=metadata,
            buttons=buttons or [],
        )

        try:
            await self._send_callback(msg)
            if channel == self._default_channel and chat_id == self._default_chat_id:
                self._sent_in_turn = True
                if resolved_media:
                    self._answered_with_files = True
                    self._answer_content = content
            media_info = f" with {len(media)} attachments" if media else ""
            return f"Message sent to {channel}:{chat_id}{media_info}"
        except Exception as e:
            return f"Error sending message: {str(e)}"

    def _resolve_media_paths(self, media: list[str]) -> list[str]:
        """Attachments must be files the bot may read: its sandbox, allowedDirs or the media folder, as
        for read_file. Otherwise anyone chatting with the bot could have it send any file on the
        machine, config.json included (custom, identity hardening)."""
        from nanobot.agent.tools.filesystem import _resolve_path

        result = []
        for m in media:
            p = Path(m).expanduser()
            if not p.is_absolute() and self._working_dir:
                p = self._working_dir / p
            result.append(str(_resolve_path(str(p), self._working_dir, self._allowed_dir, self._extra_allowed_dirs)))
        return result

    @staticmethod
    def _build_metadata(message_id: str | None, extras: dict[str, Any]) -> dict[str, Any]:
        metadata: dict[str, Any] = {"_tool_driven": True}
        if message_id:
            metadata["message_id"] = message_id

        record_delivery = extras.get("record_channel_delivery")
        if isinstance(record_delivery, bool):
            metadata["record_channel_delivery"] = record_delivery

        delivery_meta = extras.get("channel_delivery")
        if isinstance(delivery_meta, Mapping):
            metadata["channel_delivery"] = dict(delivery_meta)

        return metadata
