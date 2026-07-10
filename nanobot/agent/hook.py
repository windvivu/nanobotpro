"""Shared lifecycle hook primitives for agent runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from nanobot.providers.base import LLMResponse, ToolCallRequest


@dataclass(slots=True)
class AgentHookContext:
    """Mutable per-iteration state exposed to runner hooks."""

    iteration: int
    messages: list[dict[str, Any]]
    response: LLMResponse | None = None
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    tool_events: list[dict[str, str]] = field(default_factory=list)
    final_content: str | None = None
    stop_reason: str | None = None
    error: str | None = None
    channel: str | None = None
    chat_id: str | None = None
    message_id: str | None = None
    session_key: str | None = None
    progress_emitter: Any | None = None
    streaming_started: bool = False


class AgentHook:
    """Minimal lifecycle surface for shared runner customization."""

    def __init__(self, reraise: bool = False) -> None:
        self._reraise = reraise

    def wants_streaming(self) -> bool:
        return False

    async def before_iteration(self, context: AgentHookContext) -> None:
        pass

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        pass

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        pass

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        pass

    async def after_iteration(self, context: AgentHookContext) -> None:
        pass

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        return content


class CompositeHook(AgentHook):
    """Fan-out hook that delegates to an ordered list of hooks.

    Error isolation: async methods catch and log per-hook exceptions
    so a faulty custom hook cannot crash the agent loop.
    Hooks with ``reraise=True`` skip the try/except for transparent error propagation.
    ``finalize_content`` is a pipeline (no isolation — bugs should surface).
    """

    __slots__ = ("_hooks",)

    def __init__(self, hooks: list[AgentHook]) -> None:
        super().__init__()
        self._hooks = list(hooks)

    def wants_streaming(self) -> bool:
        return any(h.wants_streaming() for h in self._hooks)

    async def _for_each_hook_safe(self, method_name: str, *args: Any, **kwargs: Any) -> None:
        for h in self._hooks:
            if getattr(h, "_reraise", False):
                await getattr(h, method_name)(*args, **kwargs)
                continue
            try:
                await getattr(h, method_name)(*args, **kwargs)
            except Exception:
                logger.exception("AgentHook.{} error in {}", method_name, type(h).__name__)

    async def before_iteration(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("before_iteration", context)

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        await self._for_each_hook_safe("on_stream", context, delta)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        await self._for_each_hook_safe("on_stream_end", context, resuming=resuming)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("before_execute_tools", context)

    async def after_iteration(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("after_iteration", context)

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        for h in self._hooks:
            content = h.finalize_content(context, content)
        return content


class StripThinkHook(AgentHook):
    """
    Strips <think> tags from final content.
    Currently acts as a hybrid filter (only for final_content) during migration.
    """

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        if content is None:
            return None
        from nanobot.utils.helpers import strip_think
        return strip_think(content)


class PromptSnapshotHook(AgentHook):
    """
    Saves a snapshot of the prompt on the first iteration to workspace/prompt.md.
    """
    
    def __init__(self, workspace, tools):
        super().__init__()
        self._workspace = workspace
        self._tools = tools

    async def before_iteration(self, context: AgentHookContext) -> None:
        import asyncio
        from nanobot.agent.context import ContextBuilder
        
        if context.iteration == 0:
            try:
                tool_defs = self._tools.get_definitions()
                snapshot = ContextBuilder.format_prompt_snapshot(context.messages, tool_defs)
                prompt_path = self._workspace / "prompt.md"
                await asyncio.to_thread(prompt_path.write_text, snapshot, "utf-8")
            except Exception:
                pass


class VietnameseToolHintHook(AgentHook):
    """
    Sends translated tool execution hints via the progress emitter.
    """
    
    _TOOL_HINT_LABELS: dict[str, str] = {
        "web_search": "🔍 Đang tìm",
        "web_fetch": "🌐 Đang đọc",
        "read_file": "📄 Đang đọc",
        "write_file": "✏️ Đang ghi",
        "edit_file": "✏️ Đang sửa",
        "list_dir": "📁 Đang xem",
        "exec": "💻 Đang chạy",
        "message": "💬 Đang gửi",
        "spawn": "🔀 Đang tạo",
    }

    @staticmethod
    def format_hint(tool_calls: list) -> str:
        import os
        def _fmt(tc):
            label = VietnameseToolHintHook._TOOL_HINT_LABELS.get(tc.name, f"⚙️ {tc.name}")
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return label
            if os.sep in val or "/" in val:
                val = os.path.basename(val) or val
            return f'{label}("{val[:40]}…")' if len(val) > 40 else f'{label}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        if not context.progress_emitter or not context.tool_calls:
            return
            
        from nanobot.utils.helpers import strip_think
        
        # In the original _LoopHook, thought was emitted if not streaming
        # However, thought emission is logically a separate concern (progress thought).
        # We handle tool hint exclusively here.
        hint = strip_think(self.format_hint(context.tool_calls))
        if hint:
            await context.progress_emitter(hint, tool_hint=True)
