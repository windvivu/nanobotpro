"""Subagent manager for background task execution."""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.runner import AgentRunSpec, AgentRunner
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.office import (
    EditExcelTool, ReadExcelTool, ReadPdfTool, ReadWordTool, WriteExcelTool, WriteWordTool,
)
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.search import GlobTool, GrepTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ExecToolConfig, WebToolsConfig
from nanobot.providers.base import LLMProvider
from nanobot.utils.prompt_templates import render_template


@dataclass(slots=True)
class SubagentStatus:
    """Real-time status of a running subagent (used by MyTool inspect)."""

    task_id: str
    label: str
    task_description: str
    started_at: float          # time.monotonic()
    phase: str = "initializing"
    iteration: int = 0
    tool_events: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    stop_reason: str | None = None
    error: str | None = None
    current_tools: list = field(default_factory=list)  # running now, for Web Chat's panel (custom)
    # What it said and did lately (thoughts, tool calls, tool errors), for Web Chat's boxes (custom)
    progress: list = field(default_factory=list)


def _brief_argument(arguments: dict) -> str:
    """What a tool call is about (its query, url, path, pattern or command), shortened."""
    for key in ("query", "url", "path", "pattern", "command"):
        if isinstance(arguments.get(key), str):
            return arguments[key][:120]
    return next((value[:120] for value in arguments.values() if isinstance(value, str)), "")


# Sub-agents live in memory, so a restart (os.execv) or a shutdown ends them without a result. Each
# running one is listed in <workspace>/subagents/running.json with the boot id of its process; the
# next gateway start tells their chats that no result is coming (ChannelManager.start_all) (custom)
_BOOT_ID = uuid.uuid4().hex


def _running_file(workspace: Path) -> Path:
    return workspace / "subagents" / "running.json"


def _read_running(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_running(path: Path, data: dict[str, dict[str, Any]]) -> None:
    try:
        if not data:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        logger.warning("Could not update the running sub-agents list {}: {}", path, e)


def take_interrupted_subagents(workspace: Path) -> list[dict[str, Any]]:
    """Sub-agents an earlier process left running (ended by a restart or a shutdown, so no result
    will come): returns them and drops them from the list. This process's own are kept."""
    path = _running_file(workspace)
    data = _read_running(path)
    ended = {tid: rec for tid, rec in data.items() if rec.get("boot") != _BOOT_ID}
    if ended:
        _write_running(path, {tid: rec for tid, rec in data.items() if tid not in ended})
    return list(ended.values())


class _SubagentHook(AgentHook):
    """Hook for subagent execution: logs tool calls and updates the status table (as upstream)."""

    def __init__(self, task_id: str, status: SubagentStatus | None = None) -> None:
        super().__init__()
        self._task_id = task_id
        self._status = status

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        for tool_call in context.tool_calls:
            args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
            logger.debug(
                "Subagent [{}] executing: {} with arguments: {}",
                self._task_id, tool_call.name, args_str,
            )
        if self._status is not None:
            # What it says before using tools, and each call, as the bot's progress lines (custom)
            from nanobot.utils.helpers import strip_think

            thought = strip_think(context.response.content if context.response else None).strip()
            if thought:
                self._status.progress.append({"kind": "thought", "text": thought[:300]})
            for tool_call in context.tool_calls:
                self._status.progress.append({
                    "kind": "tool", "name": tool_call.name, "arg": _brief_argument(tool_call.arguments or {}),
                })
            del self._status.progress[:-12]

    async def after_iteration(self, context: AgentHookContext) -> None:
        if self._status is None:
            return
        self._status.iteration = context.iteration
        for event in context.tool_events:  # failed calls show in its progress too (custom)
            if event.get("status") == "error":
                self._status.progress.append({"kind": "error", "name": event.get("name", "?"),
                                              "text": event.get("detail", "")})
        del self._status.progress[:-12]
        # Upstream keeps only this iteration's tools and usage; here both cover the whole run, so the
        # bot can say what is already done (the `my` tool shows the last 5 tools) (custom)
        self._status.tool_events.extend(context.tool_events)
        del self._status.tool_events[:-20]
        for key, value in (context.usage or {}).items():
            if isinstance(value, (int, float)):
                self._status.usage[key] = self._status.usage.get(key, 0) + value
        if context.error:
            self._status.error = str(context.error)


class SubagentManager:
    """Manages background subagent execution."""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        max_tool_result_chars: int = 0,
        model: str | None = None,
        web_config: "WebToolsConfig | None" = None,
        exec_config: "ExecToolConfig | None" = None,
        restrict_to_workspace: bool = False,
        disabled_skills: list[str] | None = None,
        max_iterations: int | None = None,
        context_window_tokens: int | None = None,
        context_block_limit: int | None = None,
        provider_retry_mode: str = "standard",
    ):
        from nanobot.config.schema import AgentDefaults, ExecToolConfig

        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.web_config = web_config or WebToolsConfig()
        self.max_tool_result_chars = max_tool_result_chars
        self.exec_config = exec_config or ExecToolConfig()
        self.restrict_to_workspace = restrict_to_workspace
        # The bot's limits: its iteration cap (as upstream) and its context window, so a long run
        # trims its history instead of overflowing the model; AgentLoop passes them (custom)
        self.max_iterations = max_iterations if max_iterations is not None else AgentDefaults().max_tool_iterations
        self.context_window_tokens = context_window_tokens
        self.context_block_limit = context_block_limit
        self.provider_retry_mode = provider_retry_mode
        # What sub-agents may use: AgentLoop sets it from the tool role (set_tool_policy)
        self._tool_groups: frozenset[str] = frozenset({"file_read", "file_write", "shell", "web"})
        self._sandbox_mode = "workspace" if restrict_to_workspace else "unrestricted"
        self._workspace_subdir = ""
        self._allowed_dirs: list[str] = []
        self.disabled_skills = set(disabled_skills or [])
        self.runner = AgentRunner(provider)
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._task_statuses: dict[str, SubagentStatus] = {}  # task_id -> live status (`my` tool)
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}
        self._stopped_by_user: set[str] = set()  # cancelled with Stop: no "interrupted" notice later

    def statuses_for_session(self, session_key: str | None) -> dict[str, SubagentStatus]:
        """Sub-agents started from the given session only, never other chats' (as upstream)."""
        if not session_key:
            return {}
        task_ids = self._session_tasks.get(session_key, set())
        return {tid: st for tid, st in self._task_statuses.items() if tid in task_ids}

    def set_tool_policy(
        self,
        groups: frozenset[str] | set[str],
        *,
        sandbox_mode: str,
        workspace_subdir: str = "",
        allowed_dirs: list[str] | None = None,
    ) -> None:
        """Tools for sub-agents spawned from now on: the groups of the bot's tool role
        (tool_roles.subagent_groups), in the same sandbox and allowedDirs as the bot."""
        self._tool_groups = frozenset(groups)
        self._sandbox_mode = sandbox_mode
        self._workspace_subdir = workspace_subdir or ""
        self._allowed_dirs = list(allowed_dirs or [])

    def _build_tools(self) -> ToolRegistry:
        """A sub-agent's tools: never message, spawn, cron or trading; the rest per tool role."""
        from nanobot.agent.workspace_scope import resolve_workspace_scope

        groups = self._tool_groups
        tools = ToolRegistry()
        scope = resolve_workspace_scope(
            workspace=self.workspace,
            sandbox_mode=self._sandbox_mode,
            workspace_subdir=self._workspace_subdir,
            allowed_dirs=self._allowed_dirs,
        )
        if "file_read" in groups:
            tools.register(ReadFileTool(
                workspace=self.workspace,
                allowed_dir=scope.allowed_dir,
                extra_allowed_dirs=scope.extra_read_dirs,
            ))
            for cls in (ListDirTool, GlobTool, GrepTool):
                tools.register(cls(
                    workspace=self.workspace,
                    allowed_dir=scope.allowed_dir,
                    extra_allowed_dirs=scope.extra_allowed_dirs,
                ))
        if "file_write" in groups:
            for cls in (WriteFileTool, EditFileTool):
                tools.register(cls(
                    workspace=self.workspace,
                    allowed_dir=scope.allowed_dir,
                    extra_allowed_dirs=scope.extra_allowed_dirs,
                ))
        if "office" in groups:
            for cls in (ReadExcelTool, WriteExcelTool, EditExcelTool, ReadWordTool, WriteWordTool, ReadPdfTool):
                tools.register(cls(
                    workspace=self.workspace,
                    allowed_dir=scope.allowed_dir,
                    extra_allowed_dirs=scope.extra_allowed_dirs,
                ))
        if "shell" in groups and self.exec_config.enable and self._sandbox_mode != "disabled":
            tools.register(ExecTool(
                working_dir=scope.exec_working_dir,
                timeout=self.exec_config.timeout,
                sandbox_mode=self._sandbox_mode,
                allowed_dirs=self._allowed_dirs,
                path_append=self.exec_config.path_append,
                sandbox=self.exec_config.sandbox,
            ))
        if "web" in groups and self.web_config.enable:
            tools.register(WebSearchTool(config=self.web_config.search, proxy=self.web_config.proxy))
            tools.register(WebFetchTool(proxy=self.web_config.proxy))
        return tools

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
    ) -> str:
        """Spawn a subagent to execute a task in the background."""
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin = {"channel": origin_channel, "chat_id": origin_chat_id, "session_key": session_key}
        status = SubagentStatus(
            task_id=task_id,
            label=display_label,
            task_description=task,
            started_at=time.monotonic(),
        )
        self._task_statuses[task_id] = status

        bg_task = asyncio.create_task(
            self._run_subagent(task_id, task, display_label, origin, status)
        )
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)
        self._remember_running(task_id, display_label, origin)

        def _cleanup(task: asyncio.Task) -> None:
            # Finished, failed, or stopped by the user: off the list. Cancelled by a shutdown: it stays,
            # so the next start can tell the chat (custom)
            if not task.cancelled() or task_id in self._stopped_by_user:
                self._forget_running(task_id)
            self._stopped_by_user.discard(task_id)
            self._running_tasks.pop(task_id, None)
            self._task_statuses.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]

        bg_task.add_done_callback(_cleanup)

        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, Any],
        status: SubagentStatus,
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task: {}", task_id, label)

        async def _on_checkpoint(payload: dict[str, Any]) -> None:
            status.phase = payload.get("phase", status.phase)
            status.iteration = payload.get("iteration", status.iteration)
            status.current_tools = [  # the tools running now, for Web Chat's progress panel (custom)
                (call.get("function") or {}).get("name", "?") for call in payload.get("pending_tool_calls") or []
            ]

        try:
            # Sub-agent tools follow the bot's tool role (set_tool_policy)
            tools = self._build_tools()
            system_prompt = self._build_subagent_prompt()
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            result = await self.runner.run(AgentRunSpec(
                initial_messages=messages,
                tools=tools,
                model=self.model,
                max_iterations=self.max_iterations,
                max_tool_result_chars=self.max_tool_result_chars,
                context_window_tokens=self.context_window_tokens,
                context_block_limit=self.context_block_limit,
                provider_retry_mode=self.provider_retry_mode,
                hook=_SubagentHook(task_id, status),
                checkpoint_callback=_on_checkpoint,
                max_iterations_message="Task completed but no final response was generated.",
                error_message=None,
                # No fail_on_tool_error (as upstream): a failed tool call goes back to the sub-agent as
                # text and it fixes the call or moves on, instead of losing the whole task (custom)
            ))
            status.phase = "done"
            status.stop_reason = result.stop_reason
            if result.stop_reason == "tool_error":
                await self._announce_result(
                    task_id,
                    label,
                    task,
                    self._format_partial_progress(result),
                    origin,
                    "error",
                )
                return
            if result.stop_reason == "error":
                await self._announce_result(
                    task_id,
                    label,
                    task,
                    result.error or "Error: subagent execution failed.",
                    origin,
                    "error",
                )
                return
            final_result = result.final_content or "Task completed but no final response was generated."

            logger.info("Subagent [{}] completed successfully", task_id)
            await self._announce_result(task_id, label, task, final_result, origin, "ok")

        except Exception as e:
            status.phase = "error"
            status.error = str(e)
            error_msg = f"Error: {str(e)}"
            logger.error("Subagent [{}] failed: {}", task_id, e)
            await self._announce_result(task_id, label, task, error_msg, origin, "error")

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, Any],
        status: str,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        status_text = "completed successfully" if status == "ok" else "failed"

        announce_content = render_template(
            "agent/subagent_announce.md",
            label=label,
            status_text=status_text,
            task=task,
            result=result,
        )

        # Inject as system message to trigger main agent. session_key_override names the session that
        # spawned it (the unified one when unifiedSession is on): the bot answers it there, after that
        # session's turn in progress (custom, as upstream)
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
            session_key_override=origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}",
        )

        await self.bus.publish_inbound(msg)
        logger.debug("Subagent [{}] announced result to {}:{}", task_id, origin['channel'], origin['chat_id'])

    @staticmethod
    def _format_partial_progress(result) -> str:
        completed = [e for e in result.tool_events if e["status"] == "ok"]
        failure = next((e for e in reversed(result.tool_events) if e["status"] == "error"), None)
        lines: list[str] = []
        if completed:
            lines.append("Completed steps:")
            for event in completed[-3:]:
                lines.append(f"- {event['name']}: {event['detail']}")
        if failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {failure['name']}: {failure['detail']}")
        if result.error and not failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {result.error}")
        return "\n".join(lines) or (result.error or "Error: subagent execution failed.")

    def _build_subagent_prompt(self) -> str:
        """Build a focused system prompt for the subagent."""
        from nanobot.agent.context import ContextBuilder
        from nanobot.agent.skills import SkillsLoader

        time_ctx = ContextBuilder._build_runtime_context(None, None)
        skills_summary = SkillsLoader(
            self.workspace,
            disabled_skills=self.disabled_skills,
        ).build_skills_summary()
        return render_template(
            "agent/subagent_system.md",
            time_ctx=time_ctx,
            workspace=str(self.workspace),
            skills_summary=skills_summary or "",
        )

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        ids = [tid for tid in self._session_tasks.get(session_key, [])
               if tid in self._running_tasks and not self._running_tasks[tid].done()]
        tasks = [self._running_tasks[tid] for tid in ids]
        self._stopped_by_user.update(ids)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)

    def running_labels(self) -> list[str]:
        """Labels of the sub-agents still running, e.g. for the restart warning (custom)."""
        return [self._task_statuses[tid].label for tid, task in self._running_tasks.items()
                if not task.done() and tid in self._task_statuses]

    def _remember_running(self, task_id: str, label: str, origin: dict[str, Any]) -> None:
        path = _running_file(self.workspace)
        data = _read_running(path)
        data[task_id] = {"label": label, "channel": origin["channel"], "chat_id": origin["chat_id"],
                         "session_key": origin.get("session_key"), "boot": _BOOT_ID,
                         "started_at": datetime.now().isoformat(timespec="seconds")}
        _write_running(path, data)

    def _forget_running(self, task_id: str) -> None:
        path = _running_file(self.workspace)
        data = _read_running(path)
        if data.pop(task_id, None) is not None:
            _write_running(path, data)

    def get_running_count_by_session(self, session_key: str) -> int:
        """Return the number of currently running subagents for a session."""
        tids = self._session_tasks.get(session_key, set())
        return sum(
            1 for tid in tids
            if tid in self._running_tasks and not self._running_tasks[tid].done()
        )
