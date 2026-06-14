import asyncio
import logging
from typing import Any, Callable, Awaitable

from nanobot.bus.events import OutboundMessage

logger = logging.getLogger("nanobot.agent.delivery")

class ResponseDeliveryPolicy:
    """
    Handles delivery logic such as message chunking (---+---), 
    typing delay computation, and cross-routing (bus vs direct callbacks).
    """

    def __init__(self, chatbot_config: Any = None, bus: Any = None):
        self._chatbot_config = chatbot_config
        self._bus = bus

    def _split_multi_message(self, content: str) -> list[str]:
        """Split content by delimiter into parts, respecting max_splits."""
        cfg = self._chatbot_config
        if not cfg or not getattr(cfg, 'multi_message_enabled', False):
            return [content]

        delimiter = getattr(cfg, 'multi_message_delimiter', '---+---')
        if delimiter not in content:
            return [content]

        max_splits = getattr(cfg, 'max_splits', 3)
        parts = [p.strip() for p in content.split(delimiter) if p.strip()]

        if not parts:
            return [content]

        # Enforce max_splits: merge overflow parts into the last allowed part
        if len(parts) > max_splits:
            overflow = parts[max_splits - 1:]
            parts = parts[:max_splits - 1] + ["\n\n".join(overflow)]

        return parts if len(parts) > 1 else [content]

    def _compute_typing_delay(self, next_part: str) -> float:
        """Compute a natural typing delay (seconds) based on next message length."""
        cfg = self._chatbot_config
        if not cfg:
            return 0.8

        base = getattr(cfg, 'typing_delay_base_ms', 800)
        per_char = getattr(cfg, 'typing_delay_per_char_ms', 20)
        max_ms = getattr(cfg, 'typing_delay_max_ms', 3000)

        delay_ms = min(base + len(next_part) * per_char, max_ms)
        return delay_ms / 1000

    async def deliver(
        self,
        final_content: str,
        channel: str,
        chat_id: str,
        sender_id: str,
        metadata: dict,
        on_stream: Any,
        on_multi_send: Callable[[str], Awaitable[None]] | None,
        direct_return: bool,
        stop_reason: str,
    ) -> OutboundMessage:
        """
        Processes split chunks and typing delay delays, emitting early parts 
        via bus or callbacks, and returns the final OutboundMessage chunk.
        """
        # Multi-message split: only when NOT streaming
        # For bus-based callers: send intermediate parts via bus
        # For direct callers (webchat): send via on_multi_send callback
        if on_stream is None and final_content:
            parts = self._split_multi_message(final_content)
            if len(parts) > 1:
                import loguru
                loguru.logger.info(
                    "[MultiMsg] Splitting response into {} parts for {}:{}",
                    len(parts), channel, chat_id,
                )
                chunk_meta = dict(metadata or {})
                for i, part in enumerate(parts[:-1]):
                    loguru.logger.debug(
                        "[MultiMsg] Sending part {}/{} to {}:{} | thread_type={} | len={}",
                        i + 1, len(parts), channel, chat_id,
                        chunk_meta.get("thread_type", "N/A"), len(part),
                    )
                    if on_multi_send:
                        await on_multi_send(part)
                    elif not direct_return and self._bus:
                        await self._bus.publish_outbound(OutboundMessage(
                            channel=channel, chat_id=chat_id, content=part,
                            metadata=chunk_meta,
                        ))
                    delay = self._compute_typing_delay(parts[i + 1])
                    await asyncio.sleep(delay)
                final_content = parts[-1]

        import loguru
        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        loguru.logger.info("Response to {}:{}: {}", channel, sender_id, preview)

        meta = dict(metadata or {})
        if on_stream is not None and stop_reason != "error":
            meta["_streamed"] = True
            
        return OutboundMessage(
            channel=channel, chat_id=chat_id, content=final_content,
            metadata=meta,
        )
