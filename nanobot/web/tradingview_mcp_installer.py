"""Managed installer for the TradingView MCP integration."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from nanobot.config.paths import get_data_dir
from nanobot.config.schema import Config, MCPServerConfig

TRADINGVIEW_MCP_SERVER_NAME = "tradingview"
TRADINGVIEW_MCP_REPO = "https://github.com/windvivu/tradingview-mcp.git"
TRADINGVIEW_MCP_REF = "v1.0.0"
TRADINGVIEW_MCP_COMMIT = "089d3507d52c7ef7d18add3a019f2f7dc16747bf"
TRADINGVIEW_MCP_METADATA = ".nanobot-install.json"
TRADINGVIEW_MCP_ENTRYPOINT = "src/server.js"


class TradingViewMCPInstallError(RuntimeError):
    """Raised when the managed TradingView MCP backend cannot be installed."""


@dataclass(frozen=True)
class TradingViewInstallResult:
    path: str
    commit: str
    ref: str
    installed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "commit": self.commit,
            "ref": self.ref,
            "installed": self.installed,
        }


def managed_mcp_root() -> Path:
    """Return the managed MCP backend root directory."""
    root = get_data_dir() / "managed_mcp"
    root.mkdir(parents=True, exist_ok=True)
    return root


def tradingview_install_dir() -> Path:
    """Return the managed TradingView backend directory."""
    return managed_mcp_root() / TRADINGVIEW_MCP_SERVER_NAME


def _assert_inside_managed_root(path: Path) -> Path:
    root = managed_mcp_root().resolve(strict=False)
    target = path.resolve(strict=False)
    if target == root or not target.is_relative_to(root):
        raise TradingViewMCPInstallError("Refusing to touch path outside managed MCP root")
    return target


def _safe_rmtree(path: Path) -> None:
    _assert_inside_managed_root(path)
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink():
        raise TradingViewMCPInstallError("Refusing to remove symlinked managed MCP directory")
    shutil.rmtree(path, onexc=_chmod_and_retry_remove)


def _chmod_and_retry_remove(function, path: str, excinfo) -> None:
    """Handle Windows read-only files left by git/npm before rmtree gives up."""
    try:
        os.chmod(path, 0o700)
        function(path)
    except Exception:
        raise excinfo[1]


def _sanitize_output_tail(text: str, *, limit: int = 2000) -> str:
    sensitive_markers = ("token", "cookie", "authorization", "password", "secret", "api_key")
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if any(marker in line.lower() for marker in sensitive_markers):
            lines.append("[redacted sensitive output line]")
        else:
            lines.append(line)
    sanitized = "\n".join(lines).strip()
    return sanitized[-limit:]


def _run_checked(args: list[str], *, cwd: Path | None = None, timeout: int = 180) -> str:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            check=False,
        )
    except FileNotFoundError as exc:
        raise TradingViewMCPInstallError(f"Required command not found: {args[0]}") from exc
    except PermissionError as exc:
        raise TradingViewMCPInstallError(f"Permission denied running command: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + "\n" + (exc.stderr or "")
        tail = _sanitize_output_tail(output)
        raise TradingViewMCPInstallError(f"Command timed out: {args[0]}. {tail}".strip()) from exc

    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0:
        tail = _sanitize_output_tail(output)
        raise TradingViewMCPInstallError(f"Command failed: {args[0]}. {tail}".strip())
    return proc.stdout.strip()


def _require_command(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise TradingViewMCPInstallError(f"Required command not found: {name}")
    return executable


def _write_metadata(path: Path) -> None:
    payload = {
        "kind": "nanobot-managed-mcp",
        "server": TRADINGVIEW_MCP_SERVER_NAME,
        "repo": TRADINGVIEW_MCP_REPO,
        "ref": TRADINGVIEW_MCP_REF,
        "commit": TRADINGVIEW_MCP_COMMIT,
    }
    (path / TRADINGVIEW_MCP_METADATA).write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _validate_backend(path: Path, *, require_metadata: bool = False) -> None:
    missing = [
        rel
        for rel in (
            "package.json",
            "package-lock.json",
            TRADINGVIEW_MCP_ENTRYPOINT,
            "node_modules/@modelcontextprotocol/sdk",
            "node_modules/chrome-remote-interface",
        )
        if not (path / rel).exists()
    ]
    if missing:
        raise TradingViewMCPInstallError(
            "TradingView MCP backend is incomplete: " + ", ".join(missing)
        )
    if require_metadata:
        metadata_path = path / TRADINGVIEW_MCP_METADATA
        if not metadata_path.exists():
            raise TradingViewMCPInstallError("TradingView MCP backend metadata is missing")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TradingViewMCPInstallError("TradingView MCP backend metadata is invalid") from exc
        if (
            metadata.get("repo") != TRADINGVIEW_MCP_REPO
            or metadata.get("ref") != TRADINGVIEW_MCP_REF
            or metadata.get("commit") != TRADINGVIEW_MCP_COMMIT
        ):
            raise TradingViewMCPInstallError("TradingView MCP backend metadata does not match pinned source")


def install_tradingview_mcp(*, force: bool = False) -> TradingViewInstallResult:
    """Clone and install the pinned TradingView MCP backend."""
    git = _require_command("git")
    npm = _require_command("npm")
    _require_command("node")

    install_dir = tradingview_install_dir()
    if install_dir.exists() and not force:
        _assert_inside_managed_root(install_dir)
        _validate_backend(install_dir, require_metadata=True)
        return TradingViewInstallResult(
            path=str(install_dir),
            commit=TRADINGVIEW_MCP_COMMIT,
            ref=TRADINGVIEW_MCP_REF,
            installed=False,
        )

    root = managed_mcp_root()
    temp_dir = root / f".{TRADINGVIEW_MCP_SERVER_NAME}-{uuid.uuid4().hex}.tmp"
    _assert_inside_managed_root(temp_dir)

    try:
        logger.info("[TradingView MCP] Installing managed backend from pinned ref {}", TRADINGVIEW_MCP_REF)
        _run_checked(
            [
                git,
                "clone",
                "--depth",
                "1",
                "--branch",
                TRADINGVIEW_MCP_REF,
                "--single-branch",
                TRADINGVIEW_MCP_REPO,
                str(temp_dir),
            ],
            timeout=180,
        )
        head = _run_checked([git, "rev-parse", "HEAD"], cwd=temp_dir, timeout=30).strip()
        if head != TRADINGVIEW_MCP_COMMIT:
            raise TradingViewMCPInstallError("Pinned TradingView MCP commit verification failed")

        for rel in ("package.json", "package-lock.json", TRADINGVIEW_MCP_ENTRYPOINT):
            if not (temp_dir / rel).exists():
                raise TradingViewMCPInstallError(f"TradingView MCP repo is missing {rel}")

        _run_checked([npm, "ci"], cwd=temp_dir, timeout=300)
        _validate_backend(temp_dir)
        _write_metadata(temp_dir)

        if install_dir.exists():
            _safe_rmtree(install_dir)
        temp_dir.rename(install_dir)
    except Exception as original_exc:
        if temp_dir.exists() or temp_dir.is_symlink():
            try:
                _safe_rmtree(temp_dir)
            except Exception as cleanup_exc:
                logger.warning(
                    "[TradingView MCP] Failed to clean temporary install dir {}: {}",
                    temp_dir,
                    type(cleanup_exc).__name__,
                )
        raise original_exc

    return TradingViewInstallResult(
        path=str(install_dir),
        commit=TRADINGVIEW_MCP_COMMIT,
        ref=TRADINGVIEW_MCP_REF,
        installed=True,
    )


def build_tradingview_mcp_config() -> MCPServerConfig:
    """Build the runtime MCP server config for the managed TradingView backend."""
    entrypoint = tradingview_install_dir() / TRADINGVIEW_MCP_ENTRYPOINT
    node = shutil.which("node") or "node"
    return MCPServerConfig(
        enabled=True,
        type="stdio",
        command=node,
        args=[str(entrypoint)],
        env={},
        tool_timeout=30,
        enabled_tools=["*"],
    )


def uninstall_tradingview_mcp_config(config: Config) -> bool:
    """Remove only the MCP config entry, keeping the backend cache by default."""
    return config.tools.mcp_servers.pop(TRADINGVIEW_MCP_SERVER_NAME, None) is not None


async def tradingview_mcp_health(config: Config) -> dict[str, Any]:
    """Return secret-free readiness checks for the managed TradingView MCP integration."""
    install_dir = tradingview_install_dir()
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    add("node", shutil.which("node") is not None, "node available" if shutil.which("node") else "node not found")
    add("backend", (install_dir / TRADINGVIEW_MCP_ENTRYPOINT).exists(), "backend installed")
    add(
        "dependencies",
        (install_dir / "node_modules/@modelcontextprotocol/sdk").exists()
        and (install_dir / "node_modules/chrome-remote-interface").exists(),
        "node_modules ready",
    )
    add(
        "config",
        TRADINGVIEW_MCP_SERVER_NAME in config.tools.mcp_servers,
        "MCP server configured",
    )

    cdp_detail = "Chrome CDP not reachable"
    cdp_ok = False
    browser_version: dict[str, str] = {}
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            response = await client.get("http://127.0.0.1:9222/json/version")
            response.raise_for_status()
            payload = response.json()
            browser_version = {
                key: str(payload[key])
                for key in ("Browser", "Protocol-Version")
                if key in payload
            }
            cdp_ok = True
            cdp_detail = "Chrome CDP reachable"
    except Exception:
        pass
    add("chrome_cdp", cdp_ok, cdp_detail)

    return {
        "success": True,
        "ok": all(item["ok"] for item in checks),
        "server_name": TRADINGVIEW_MCP_SERVER_NAME,
        "repo": TRADINGVIEW_MCP_REPO,
        "ref": TRADINGVIEW_MCP_REF,
        "commit": TRADINGVIEW_MCP_COMMIT,
        "installed_path": str(install_dir),
        "checks": checks,
        "browser_version": browser_version,
    }
