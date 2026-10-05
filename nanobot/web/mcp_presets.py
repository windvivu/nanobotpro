"""Pure configuration helpers for dashboard-managed MCP preset bundles."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from nanobot.config.schema import (
    MCPBundleInstallationConfig,
    MCPPresetBundleConfig,
    MCPServerConfig,
    ToolsConfig,
)

_PRESET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class MCPPresetError(ValueError):
    """Base error for invalid MCP preset operations."""


class MCPPresetConflictError(MCPPresetError):
    """Raised when an operation would overwrite existing MCP configuration."""


class MCPPresetInUseError(MCPPresetError):
    """Raised when deleting a custom preset that is still installed."""


BUILTIN_MCP_PRESETS: dict[str, MCPPresetBundleConfig] = {
    "playwright_headless": MCPPresetBundleConfig(
        label="Playwright (Headless)",
        description="Browser automation suitable for headless and Docker environments.",
        servers={
            "browser": MCPServerConfig(
                type="stdio",
                command="npx",
                args=[
                    "-y",
                    "@playwright/mcp@latest",
                    "--headless",
                    "--output-dir",
                    ".workspace/sandbox/browser-logs",
                    "--user-data-dir",
                    ".workspace/sandbox/browser-data",
                ],
                tool_timeout=60,
            )
        },
    ),
    "playwright_visible": MCPPresetBundleConfig(
        label="Playwright (Visible)",
        description="Browser automation with a visible browser window.",
        servers={
            "browser": MCPServerConfig(
                type="stdio",
                command="npx",
                args=[
                    "-y",
                    "@playwright/mcp@latest",
                    "--output-dir",
                    ".workspace/sandbox/browser-logs",
                    "--user-data-dir",
                    ".workspace/sandbox/browser-data",
                ],
                tool_timeout=60,
            )
        },
    ),
}

# The oldest Node.js major a built-in preset's servers run on (custom). Playwright MCP exits at once
# on anything older than 20. The dashboard makes sure there is one before it installs or tests the
# preset, downloading nanobot's own when the machine's will not do (nanobot/web/managed_node.py).
MCP_PRESET_MIN_NODE: dict[str, int] = {"playwright_headless": 20, "playwright_visible": 20}


def validate_preset_id(preset_id: str) -> str:
    """Validate and return a canonical MCP preset identifier."""
    normalized = preset_id.strip()
    if not _PRESET_ID_RE.fullmatch(normalized):
        raise MCPPresetError("Preset id must match [a-z0-9][a-z0-9_-]{0,63}")
    return normalized


def _validate_bundle(preset_id: str, bundle: MCPPresetBundleConfig) -> None:
    validate_preset_id(preset_id)
    if not bundle.servers:
        raise MCPPresetError("MCP preset bundle must contain at least one server")
    for server_name, server in bundle.servers.items():
        validate_preset_id(server_name)
        if server.type == "stdio" and not server.command:
            raise MCPPresetError(f"MCP server '{server_name}' requires a command")
        if server.type in {"sse", "streamableHttp"} and not server.url:
            raise MCPPresetError(f"MCP server '{server_name}' requires a URL")
        if not server.command and not server.url:
            raise MCPPresetError(f"MCP server '{server_name}' requires a command or URL")
        if server.url and urlparse(server.url).scheme not in {"http", "https"}:
            raise MCPPresetError(f"MCP server '{server_name}' URL must use http or https")
        if not 1 <= server.tool_timeout <= 300:
            raise MCPPresetError(f"MCP server '{server_name}' timeout must be between 1 and 300 seconds")


def _get_bundle(tools: ToolsConfig, preset_id: str) -> MCPPresetBundleConfig:
    preset_id = validate_preset_id(preset_id)
    bundle = BUILTIN_MCP_PRESETS.get(preset_id) or tools.mcp_custom_presets.get(preset_id)
    if bundle is None:
        raise MCPPresetError(f"MCP preset '{preset_id}' is not defined")
    _validate_bundle(preset_id, bundle)
    return bundle


def list_mcp_presets(tools: ToolsConfig) -> dict[str, dict[str, object]]:
    """Return built-in and custom preset metadata with installation state."""
    result: dict[str, dict[str, object]] = {}
    for source, presets in (("builtin", BUILTIN_MCP_PRESETS), ("custom", tools.mcp_custom_presets)):
        for preset_id, bundle in presets.items():
            installation = tools.mcp_preset_installations.get(preset_id)
            result[preset_id] = {
                "source": source,
                "bundle": bundle.model_copy(deep=True),
                "installed": installation is not None,
                "enabled": installation.enabled if installation else False,
                "server_names": list(installation.server_names) if installation else [],
            }
    return result


def install_mcp_preset(tools: ToolsConfig, preset_id: str) -> MCPBundleInstallationConfig:
    """Materialize a preset into mcp_servers after validating all collisions."""
    preset_id = validate_preset_id(preset_id)
    if preset_id in tools.mcp_preset_installations:
        raise MCPPresetConflictError(f"MCP preset '{preset_id}' is already installed")
    bundle = _get_bundle(tools, preset_id)
    collisions = sorted(name for name in bundle.servers if name in tools.mcp_servers)
    if collisions:
        raise MCPPresetConflictError(
            f"MCP preset '{preset_id}' conflicts with existing servers: {', '.join(collisions)}"
        )

    installation = MCPBundleInstallationConfig(
        preset_id=preset_id,
        server_names=list(bundle.servers),
        enabled=True,
    )
    for name, server in bundle.servers.items():
        materialized = server.model_copy(deep=True)
        materialized.enabled = True
        tools.mcp_servers[name] = materialized
    tools.mcp_preset_installations[preset_id] = installation
    return installation


def set_mcp_preset_enabled(
    tools: ToolsConfig,
    preset_id: str,
    enabled: bool,
) -> MCPBundleInstallationConfig:
    """Enable or disable every server owned by an installed preset."""
    preset_id = validate_preset_id(preset_id)
    installation = tools.mcp_preset_installations.get(preset_id)
    if installation is None:
        raise MCPPresetError(f"MCP preset '{preset_id}' is not installed")
    missing = sorted(name for name in installation.server_names if name not in tools.mcp_servers)
    if missing:
        raise MCPPresetError(
            f"MCP preset '{preset_id}' is missing installed servers: {', '.join(missing)}"
        )

    for name in installation.server_names:
        tools.mcp_servers[name].enabled = enabled
    installation.enabled = enabled
    return installation


def uninstall_mcp_preset(tools: ToolsConfig, preset_id: str) -> None:
    """Remove only servers recorded as belonging to an installed preset."""
    preset_id = validate_preset_id(preset_id)
    installation = tools.mcp_preset_installations.get(preset_id)
    if installation is None:
        raise MCPPresetError(f"MCP preset '{preset_id}' is not installed")
    missing = sorted(name for name in installation.server_names if name not in tools.mcp_servers)
    if missing:
        raise MCPPresetError(
            f"MCP preset '{preset_id}' is missing installed servers: {', '.join(missing)}"
        )

    for name in installation.server_names:
        del tools.mcp_servers[name]
    del tools.mcp_preset_installations[preset_id]


def save_custom_mcp_preset(
    tools: ToolsConfig,
    preset_id: str,
    bundle: MCPPresetBundleConfig,
) -> None:
    """Create or replace a custom preset definition that is not installed."""
    preset_id = validate_preset_id(preset_id)
    if preset_id in BUILTIN_MCP_PRESETS:
        raise MCPPresetConflictError(f"MCP preset id '{preset_id}' is reserved")
    if preset_id in tools.mcp_preset_installations:
        raise MCPPresetInUseError(f"Uninstall MCP preset '{preset_id}' before editing it")
    _validate_bundle(preset_id, bundle)
    tools.mcp_custom_presets[preset_id] = bundle.model_copy(deep=True)


def delete_custom_mcp_preset(tools: ToolsConfig, preset_id: str) -> None:
    """Delete a custom preset definition when it is not installed."""
    preset_id = validate_preset_id(preset_id)
    if preset_id in tools.mcp_preset_installations:
        raise MCPPresetInUseError(f"Uninstall MCP preset '{preset_id}' before deleting it")
    if preset_id not in tools.mcp_custom_presets:
        raise MCPPresetError(f"Custom MCP preset '{preset_id}' is not defined")
    del tools.mcp_custom_presets[preset_id]


def adopt_exact_builtin_preset(tools: ToolsConfig, preset_id: str) -> MCPBundleInstallationConfig:
    """Adopt exact legacy built-in server configs without changing runtime data."""
    preset_id = validate_preset_id(preset_id)
    if preset_id in tools.mcp_preset_installations:
        raise MCPPresetConflictError(f"MCP preset '{preset_id}' is already installed")
    bundle = BUILTIN_MCP_PRESETS.get(preset_id)
    if bundle is None:
        raise MCPPresetError(f"Built-in MCP preset '{preset_id}' is not defined")
    if set(bundle.servers) - set(tools.mcp_servers):
        raise MCPPresetError(f"Configured MCP servers do not exactly match preset '{preset_id}'")
    for name, expected in bundle.servers.items():
        actual = tools.mcp_servers[name]
        if actual.model_dump(mode="json") != expected.model_dump(mode="json"):
            raise MCPPresetError(f"Configured MCP servers do not exactly match preset '{preset_id}'")

    installation = MCPBundleInstallationConfig(
        preset_id=preset_id,
        server_names=list(bundle.servers),
        enabled=all(tools.mcp_servers[name].enabled for name in bundle.servers),
    )
    tools.mcp_preset_installations[preset_id] = installation
    return installation
