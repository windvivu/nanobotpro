"""RuntimeState protocol: agent loop state exposed to MyTool."""

from typing import Any, Protocol


class RuntimeState(Protocol):
    """Minimum contract that MyTool requires from its runtime state provider."""

    @property
    def model(self) -> str: ...

    @property
    def max_iterations(self) -> int: ...

    @property
    def current_iteration(self) -> int: ...

    @property
    def tool_names(self) -> list[str]: ...

    @property
    def workspace(self) -> str: ...

    @property
    def provider_retry_mode(self) -> str: ...

    @property
    def max_tool_result_chars(self) -> int: ...

    @property
    def context_window_tokens(self) -> int: ...

    @property
    def web_config(self) -> Any: ...

    @property
    def exec_config(self) -> Any: ...

    @property
    def subagents(self) -> Any: ...

    @property
    def _runtime_vars(self) -> dict[str, Any]: ...

    @property
    def _last_usage(self) -> Any: ...

    def _sync_subagent_runtime_limits(self) -> None: ...

    @property
    def model_preset(self) -> str | None: ...

    _active_preset: str | None
