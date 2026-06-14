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
    return type(exc).__name__ in _TRANSIENT_EXC_NAMES


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


class MCPToolWrapper(Tool):
    """Wraps a single MCP server tool as a nanobot Tool."""

    def __init__(self, session, server_name: str, tool_def, tool_timeout: int = 30):
        self._session = session
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

        for attempt in range(2):
            try:
                result = await asyncio.wait_for(
                    self._session.call_tool(self._original_name, arguments=kwargs),
                    timeout=self._tool_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("MCP tool '{}' timed out after {}s", self._name, self._tool_timeout)
                return f"(MCP tool call timed out after {self._tool_timeout}s)"
            except asyncio.CancelledError:
                # MCP SDK's anyio cancel scopes can leak CancelledError on timeout/failure.
                # Re-raise only if our task was externally cancelled (e.g. /stop).
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                logger.warning("MCP tool '{}' was cancelled by server/SDK", self._name)
                return "(MCP tool call was cancelled)"
            except Exception as exc:
                if _is_transient(exc):
                    if attempt == 0:
                        logger.warning(
                            "MCP tool '{}' hit transient error ({}), retrying once...",
                            self._name,
                            type(exc).__name__,
                        )
                        await asyncio.sleep(1)
                        continue
                    logger.error(
                        "MCP tool '{}' failed after retry: {}: {}",
                        self._name,
                        type(exc).__name__,
                        exc,
                    )
                    return f"(MCP tool call failed after retry: {type(exc).__name__})"
                logger.exception(
                    "MCP tool '{}' failed: {}: {}",
                    self._name,
                    type(exc).__name__,
                    exc,
                )
                return f"(MCP tool call failed: {type(exc).__name__})"
            else:
                parts = []
                for block in result.content:
                    if isinstance(block, types.TextContent):
                        parts.append(block.text)
                    else:
                        parts.append(str(block))
                return "\n".join(parts) or "(no output)"

        return "(MCP tool call failed)"


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
) -> None:
    """Connect to configured MCP servers and register their tools."""
    for name, cfg in mcp_servers.items():
        if not cfg.enabled:
            logger.info("MCP server '{}': disabled, skipping", name)
            continue
        try:
            _transport_type, session = await _open_mcp_session(cfg, stack)

            tools = await session.list_tools()
            enabled_tools = set(cfg.enabled_tools)
            allow_all_tools = "*" in enabled_tools
            registered_count = 0
            matched_enabled_tools: set[str] = set()
            available_raw_names = [tool_def.name for tool_def in tools.tools]
            available_wrapped_names = [f"mcp_{name}_{tool_def.name}" for tool_def in tools.tools]
            for tool_def in tools.tools:
                wrapped_name = f"mcp_{name}_{tool_def.name}"
                if (
                    not allow_all_tools
                    and tool_def.name not in enabled_tools
                    and wrapped_name not in enabled_tools
                ):
                    logger.debug(
                        "MCP: skipping tool '{}' from server '{}' (not in enabledTools)",
                        wrapped_name,
                        name,
                    )
                    continue
                wrapper = MCPToolWrapper(session, name, tool_def, tool_timeout=cfg.tool_timeout)
                registry.register(wrapper)
                logger.debug("MCP: registered tool '{}' from server '{}'", wrapper.name, name)
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
                        name,
                        ", ".join(unmatched_enabled_tools),
                        ", ".join(available_raw_names) or "(none)",
                        ", ".join(available_wrapped_names) or "(none)",
                    )

            logger.info("MCP server '{}': connected, {} tools registered", name, registered_count)
        except MCPConfigurationError as e:
            logger.warning("MCP server '{}': {}, skipping", name, e)
        except Exception as e:
            logger.error("MCP server '{}': failed to connect: {}", name, e)
