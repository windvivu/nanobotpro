"""MCP client: connects to MCP servers and wraps their tools as native nanobot tools."""

import asyncio
import os
import re
import shutil
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass
from time import monotonic
from typing import Any

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry

_TRANSIENT_EXC_NAMES: frozenset[str] = frozenset((
    "ClosedResourceError",
    "BrokenResourceError",
    "EndOfStream",
    "BrokenPipeError",
    "ConnectionResetError",
    "ConnectionRefusedError",
    "ConnectionAbortedError",
    "ConnectionError",
))

_WINDOWS_SHELL_LAUNCHERS: frozenset[str] = frozenset(("npx", "npm", "pnpm", "yarn", "bunx"))
_SANITIZE_RE = re.compile(r"_+")


class MCPConfigurationError(ValueError):
    """Raised when an MCP server has no usable transport configuration."""


@dataclass(frozen=True)
class MCPInspectionResult:
    """Secret-free result returned by an isolated MCP connection inspection."""

    success: bool
    transport: str = ""
    tools: tuple[dict[str, str], ...] = ()
    elapsed_ms: int = 0
    error_category: str = ""
    error_message: str = ""

    @property
    def tool_count(self) -> int:
        return len(self.tools)

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["tools"] = list(self.tools)
        result["tool_count"] = self.tool_count
        return result


def _sanitize_name(name: str) -> str:
    """Sanitize an MCP-derived name for model API compatibility."""
    return _SANITIZE_RE.sub("_", re.sub(r"[^a-zA-Z0-9_-]", "_", name))


def _is_transient(exc: BaseException) -> bool:
    """Check if an exception looks like a transient connection error."""
    if isinstance(exc, (TimeoutError, httpx.HTTPError)):
        return True
    return type(exc).__name__ in _TRANSIENT_EXC_NAMES or type(exc).__name__.endswith("McpError")


def _http_status(exc: BaseException) -> int | None:
    """Extract an HTTP status code without logging response bodies or headers."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
        status = getattr(current, "status_code", None)
        if isinstance(status, int):
            return status
        current = current.__cause__ if isinstance(current.__cause__, BaseException) else None
    return None


def _log_mcp_error(
    *,
    server_name: str,
    tool_name: str,
    phase: str,
    exc: BaseException,
    retry_attempted: bool,
    level: str = "warning",
) -> None:
    """Log MCP failures with routing detail but without secrets or payloads."""
    status = _http_status(exc)
    logger.log(
        level.upper(),
        "MCP server='{}' tool='{}' phase='{}' error_class='{}' http_status={} retry_attempted={}",
        server_name,
        tool_name or "(none)",
        phase,
        type(exc).__name__,
        status if status is not None else "(none)",
        retry_attempted,
    )


def _windows_command_basename(command: str) -> str:
    """Return the lowercase basename for a Windows command or path."""
    return command.replace("\\", "/").rsplit("/", maxsplit=1)[-1].lower()


def _normalize_windows_stdio_command(
    command: str,
    args: list[str] | None,
    env: dict[str, str] | None,
) -> tuple[str, list[str], dict[str, str] | None]:
    """Wrap Windows shell launchers so MCP stdio servers start reliably."""
    normalized_args = list(args or [])
    if os.name != "nt":
        return command, normalized_args, env

    basename = _windows_command_basename(command)
    if basename in {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return command, normalized_args, env

    if basename.endswith((".exe", ".com")):
        return command, normalized_args, env

    resolved = shutil.which(command, path=(env or {}).get("PATH")) or command
    resolved_basename = _windows_command_basename(resolved)
    should_wrap = (
        basename in _WINDOWS_SHELL_LAUNCHERS
        or basename.endswith((".cmd", ".bat"))
        or resolved_basename.endswith((".cmd", ".bat"))
    )
    if not should_wrap:
        return command, normalized_args, env

    comspec = (env or {}).get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
    return comspec, ["/d", "/c", command, *normalized_args], env


def _extract_nullable_branch(options: Any) -> tuple[dict[str, Any], bool] | None:
    """Return the single non-null branch for nullable unions."""
    if not isinstance(options, list):
        return None

    non_null: list[dict[str, Any]] = []
    saw_null = False
    for option in options:
        if not isinstance(option, dict):
            return None
        if option.get("type") == "null":
            saw_null = True
            continue
        non_null.append(option)

    if saw_null and len(non_null) == 1:
        return non_null[0], True
    return None


def _normalize_schema_for_openai(schema: Any) -> dict[str, Any]:
    """Normalize only nullable JSON Schema patterns for tool definitions."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    normalized = dict(schema)

    raw_type = normalized.get("type")
    if isinstance(raw_type, list):
        non_null = [item for item in raw_type if item != "null"]
        if "null" in raw_type and len(non_null) == 1:
            normalized["type"] = non_null[0]
            normalized["nullable"] = True

    for key in ("oneOf", "anyOf"):
        nullable_branch = _extract_nullable_branch(normalized.get(key))
        if nullable_branch is not None:
            branch, _ = nullable_branch
            merged = {k: v for k, v in normalized.items() if k != key}
            merged.update(branch)
            normalized = merged
            normalized["nullable"] = True
            break

    if "properties" in normalized and isinstance(normalized["properties"], dict):
        normalized["properties"] = {
            name: _normalize_schema_for_openai(prop)
            if isinstance(prop, dict)
            else prop
            for name, prop in normalized["properties"].items()
        }

    if "items" in normalized and isinstance(normalized["items"], dict):
        normalized["items"] = _normalize_schema_for_openai(normalized["items"])

    if normalized.get("type") != "object":
        return normalized

    normalized.setdefault("properties", {})
    normalized.setdefault("required", [])
    return normalized


class MCPServerRuntime:
    """Owns one named MCP server session and its registered tools."""

    def __init__(
        self,
        *,
        server_name: str,
        cfg: Any,
        registry: ToolRegistry,
    ) -> None:
        self.server_name = server_name
        self.cfg = cfg
        self.registry = registry
        self._stack: AsyncExitStack | None = None
        self._session: Any | None = None
        self._transport_type = ""
        self._registered_tool_names: set[str] = set()
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open a fresh session, list tools, and register wrappers for this server."""
        async with self._lock:
            await self._close_locked(unregister_tools=True)
            stack = AsyncExitStack()
            try:
                await stack.__aenter__()
                logger.debug(
                    "MCP server='{}' tool='(none)' phase='initialize' retry_attempted=False",
                    self.server_name,
                )
                transport_type, session = await _open_mcp_session(self.cfg, stack)
                logger.debug(
                    "MCP server='{}' tool='(none)' phase='list_tools' retry_attempted=False",
                    self.server_name,
                )
                tools = await session.list_tools()
                self._stack = stack
                self._session = session
                self._transport_type = transport_type
                self._register_tools(tools)
            except Exception as exc:
                _log_mcp_error(
                    server_name=self.server_name,
                    tool_name="",
                    phase="initialize/list_tools",
                    exc=exc,
                    retry_attempted=False,
                    level="error",
                )
                try:
                    await stack.aclose()
                except Exception:
                    pass
                raise

    async def close(self) -> None:
        """Close this server session and unregister only this server's tools."""
        async with self._lock:
            await self._close_locked(unregister_tools=True)

    async def call_tool(
        self,
        original_name: str,
        arguments: dict[str, Any],
        wrapper_name: str,
        timeout: int,
    ) -> Any:
        """Call a tool, reconnecting this server once if the session is stale."""
        from mcp import types  # noqa: F401  # imported here so tests can fake mcp module lazily

        for attempt in range(2):
            call_task: asyncio.Task | None = None
            try:
                if self._session is None:
                    raise ConnectionError("MCP session is not connected")
                call_task = asyncio.create_task(
                    self._session.call_tool(original_name, arguments=arguments)
                )
                return await asyncio.wait_for(call_task, timeout=timeout)
            except asyncio.TimeoutError as exc:
                if call_task is not None and not call_task.done():
                    call_task.cancel()
                    await asyncio.gather(call_task, return_exceptions=True)
                _log_mcp_error(
                    server_name=self.server_name,
                    tool_name=wrapper_name,
                    phase="call_tool",
                    exc=exc,
                    retry_attempted=attempt > 0,
                )
                if attempt == 1:
                    return f"(MCP tool call timed out after {timeout}s)"
                try:
                    await self.reconnect()
                except Exception as reconnect_exc:
                    _log_mcp_error(
                        server_name=self.server_name,
                        tool_name=wrapper_name,
                        phase="retry",
                        exc=reconnect_exc,
                        retry_attempted=True,
                        level="error",
                    )
                    return f"(MCP tool call failed after reconnect: {type(reconnect_exc).__name__})"
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                _log_mcp_error(
                    server_name=self.server_name,
                    tool_name=wrapper_name,
                    phase="call_tool",
                    exc=asyncio.CancelledError(),
                    retry_attempted=attempt > 0,
                )
                return "(MCP tool call was cancelled)"
            except Exception as exc:
                if not _is_transient(exc):
                    _log_mcp_error(
                        server_name=self.server_name,
                        tool_name=wrapper_name,
                        phase="call_tool",
                        exc=exc,
                        retry_attempted=attempt > 0,
                        level="error",
                    )
                    return f"(MCP tool call failed: {type(exc).__name__})"
                if attempt == 1:
                    _log_mcp_error(
                        server_name=self.server_name,
                        tool_name=wrapper_name,
                        phase="retry",
                        exc=exc,
                        retry_attempted=True,
                        level="error",
                    )
                    if isinstance(exc, asyncio.TimeoutError):
                        return f"(MCP tool call timed out after {timeout}s)"
                    return f"(MCP tool call failed after retry: {type(exc).__name__})"

                _log_mcp_error(
                    server_name=self.server_name,
                    tool_name=wrapper_name,
                    phase="call_tool",
                    exc=exc,
                    retry_attempted=False,
                )
                try:
                    await self.reconnect()
                except Exception as reconnect_exc:
                    _log_mcp_error(
                        server_name=self.server_name,
                        tool_name=wrapper_name,
                        phase="retry",
                        exc=reconnect_exc,
                        retry_attempted=True,
                        level="error",
                    )
                    return f"(MCP tool call failed after reconnect: {type(reconnect_exc).__name__})"

        return "(MCP tool call failed)"

    async def reconnect(self) -> None:
        """Reconnect this server only and refresh its tool registrations."""
        logger.info(
            "MCP server='{}' phase='retry' reconnecting named server only",
            self.server_name,
        )
        await self.connect()

    def _register_tools(self, tools: Any) -> None:
        enabled_tools = set(self.cfg.enabled_tools)
        allow_all_tools = "*" in enabled_tools
        registered_count = 0
        matched_enabled_tools: set[str] = set()
        available_raw_names = [tool_def.name for tool_def in tools.tools]
        available_wrapped_names = [
            f"mcp_{self.server_name}_{tool_def.name}" for tool_def in tools.tools
        ]

        for tool_def in tools.tools:
            wrapped_name = f"mcp_{self.server_name}_{tool_def.name}"
            if (
                not allow_all_tools
                and tool_def.name not in enabled_tools
                and wrapped_name not in enabled_tools
            ):
                logger.debug(
                    "MCP: skipping tool '{}' from server '{}' (not in enabledTools)",
                    wrapped_name,
                    self.server_name,
                )
                continue
            wrapper = MCPToolWrapper(self, self.server_name, tool_def, tool_timeout=self.cfg.tool_timeout)
            self.registry.register(wrapper)
            self._registered_tool_names.add(wrapper.name)
            logger.debug("MCP: registered tool '{}' from server '{}'", wrapper.name, self.server_name)
            registered_count += 1
            if enabled_tools:
                if tool_def.name in enabled_tools:
                    matched_enabled_tools.add(tool_def.name)
                if wrapped_name in enabled_tools:
                    matched_enabled_tools.add(wrapped_name)

        if enabled_tools and not allow_all_tools:
            unmatched_enabled_tools = sorted(enabled_tools - matched_enabled_tools)
            if unmatched_enabled_tools:
                logger.warning(
                    "MCP server '{}': enabledTools entries not found: {}. Available raw names: {}. "
                    "Available wrapped names: {}",
                    self.server_name,
                    ", ".join(unmatched_enabled_tools),
                    ", ".join(available_raw_names) or "(none)",
                    ", ".join(available_wrapped_names) or "(none)",
                )

        logger.info(
            "MCP server '{}': connected, {} tools registered",
            self.server_name,
            registered_count,
        )

    async def _close_locked(self, *, unregister_tools: bool) -> None:
        if unregister_tools:
            for tool_name in sorted(self._registered_tool_names):
                self.registry.unregister(tool_name)
            self._registered_tool_names.clear()
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except (RuntimeError, BaseExceptionGroup, asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self._stack = None
        self._session = None
        self._transport_type = ""


class MCPToolWrapper(Tool):
    """Wraps a single MCP server tool as a nanobot Tool."""

    def __init__(self, runtime: MCPServerRuntime, server_name: str, tool_def, tool_timeout: int = 30):
        self._runtime = runtime
        self._server_name = server_name
        self._original_name = tool_def.name
        self._name = _sanitize_name(f"mcp_{server_name}_{tool_def.name}")
        self._description = tool_def.description or tool_def.name
        raw_schema = tool_def.inputSchema or {"type": "object", "properties": {}}
        self._parameters = _normalize_schema_for_openai(raw_schema)
        self._tool_timeout = tool_timeout

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types

        result = await self._runtime.call_tool(
            self._original_name,
            kwargs,
            self._name,
            self._tool_timeout,
        )
        if isinstance(result, str):
            return result

        parts = []
        for block in result.content:
            if isinstance(block, types.TextContent):
                parts.append(block.text)
            else:
                parts.append(str(block))
        return "\n".join(parts) or "(no output)"


def _resolve_mcp_transport(cfg: Any) -> str:
    """Resolve the effective transport without exposing connection details."""
    if cfg.type:
        return cfg.type
    if cfg.command:
        return "stdio"
    if cfg.url:
        return "sse" if cfg.url.rstrip("/").endswith("/sse") else "streamableHttp"
    raise MCPConfigurationError("no command or URL configured")


async def _open_mcp_session(cfg: Any, stack: AsyncExitStack) -> tuple[str, Any]:
    """Open and initialize one MCP session using the shared runtime transport path."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client

    transport_type = _resolve_mcp_transport(cfg)
    if transport_type == "stdio":
        command, args, env = _normalize_windows_stdio_command(
            cfg.command,
            cfg.args,
            cfg.env or None,
        )
        params = StdioServerParameters(command=command, args=args, env=env)
        read, write = await stack.enter_async_context(stdio_client(params))
    elif transport_type == "sse":

        def httpx_client_factory(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            merged_headers = {
                "Accept": "application/json, text/event-stream",
                **(cfg.headers or {}),
                **(headers or {}),
            }
            return httpx.AsyncClient(
                headers=merged_headers or None,
                follow_redirects=True,
                timeout=timeout,
                auth=auth,
            )

        read, write = await stack.enter_async_context(
            sse_client(cfg.url, httpx_client_factory=httpx_client_factory)
        )
    elif transport_type == "streamableHttp":
        http_client = await stack.enter_async_context(
            httpx.AsyncClient(
                headers=cfg.headers or None,
                follow_redirects=True,
                timeout=None,
            )
        )
        read, write, _ = await stack.enter_async_context(
            streamable_http_client(cfg.url, http_client=http_client)
        )
    else:
        raise MCPConfigurationError(f"unknown transport type '{transport_type}'")

    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return transport_type, session


def _inspection_error(exc: BaseException) -> tuple[str, str]:
    """Map an exception to a stable message that cannot echo credentials."""
    if isinstance(exc, MCPConfigurationError):
        return "configuration", str(exc)
    if isinstance(exc, ImportError):
        return "dependency", "MCP client dependency is unavailable"
    if _is_transient(exc):
        return "connection", f"Connection failed ({type(exc).__name__})"
    return "connection", f"MCP inspection failed ({type(exc).__name__})"


async def inspect_mcp_server(
    name: str,
    cfg: Any,
    *,
    timeout_seconds: float = 15.0,
) -> MCPInspectionResult:
    """Inspect one MCP server in an isolated stack without registering live tools."""
    del name  # Reserved for future observability; never included in transport errors.
    started = monotonic()

    async def _inspect() -> tuple[str, Any]:
        async with AsyncExitStack() as stack:
            transport, session = await _open_mcp_session(cfg, stack)
            listed = await session.list_tools()
            tools = tuple(
                {
                    "name": str(tool.name),
                    "description": " ".join(str(tool.description or "").split())[:160],
                }
                for tool in listed.tools
            )
            return transport, tools

    try:
        transport, tools = await asyncio.wait_for(
            _inspect(),
            timeout=max(0.1, min(float(timeout_seconds), 120.0)),
        )
    except TimeoutError:
        return MCPInspectionResult(
            success=False,
            elapsed_ms=round((monotonic() - started) * 1000),
            error_category="timeout",
            error_message="MCP inspection timed out",
        )
    except Exception as exc:
        category, message = _inspection_error(exc)
        return MCPInspectionResult(
            success=False,
            elapsed_ms=round((monotonic() - started) * 1000),
            error_category=category,
            error_message=message,
        )

    return MCPInspectionResult(
        success=True,
        transport=transport,
        tools=tools,
        elapsed_ms=round((monotonic() - started) * 1000),
    )


async def connect_mcp_servers(
    mcp_servers: dict, registry: ToolRegistry, stack: AsyncExitStack
) -> dict[str, MCPServerRuntime]:
    """Connect to configured MCP servers and register their tools."""
    runtimes: dict[str, MCPServerRuntime] = {}
    for name, cfg in mcp_servers.items():
        if not cfg.enabled:
            logger.info("MCP server '{}': disabled, skipping", name)
            continue
        try:
            runtime = MCPServerRuntime(server_name=name, cfg=cfg, registry=registry)
            await runtime.connect()
            stack.push_async_callback(runtime.close)
            runtimes[name] = runtime
        except MCPConfigurationError as e:
            logger.warning("MCP server '{}': {}, skipping", name, e)
        except Exception as e:
            logger.error("MCP server '{}': failed to connect: {}", name, type(e).__name__)
    return runtimes
