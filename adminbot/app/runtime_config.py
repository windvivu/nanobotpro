"""Per-bot config initialization and synchronization."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from adminbot.app.utils import atomic_write_json


def resolve_workspace_path(input_path: str, cwd: Path) -> Path:
    value = input_path.strip()
    if not value:
        raise ValueError("Workspace path cannot be empty.")

    if (
        (value.startswith('"') and value.endswith('"'))
        or (value.startswith("'") and value.endswith("'"))
    ):
        value = value[1:-1]

    value = os_expandvars(value)
    value = value.replace("/", "\\") if "\\" in str(cwd) else value

    if value == "~":
        resolved = Path.home()
    elif value.startswith("~/") or value.startswith("~\\"):
        resolved = Path.home() / value[2:]
    elif value == "$HOME":
        resolved = Path.home()
    elif value.startswith("$HOME/") or value.startswith("$HOME\\"):
        resolved = Path.home() / value[6:]
    else:
        resolved = Path(value)
        if not resolved.is_absolute():
            resolved = cwd / resolved

    return resolved.expanduser().resolve()


def os_expandvars(value: str) -> str:
    import os

    return os.path.expandvars(value)


def initialize_bot_config(
    python_executable: Path,
    repo_root: Path,
    config_path: Path,
    workspace_path: Path,
    web_port: int,
) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(python_executable),
        "-m",
        "nanobot.cli.commands",
        "onboard",
        "--config",
        str(config_path),
        "--workspace",
        str(workspace_path),
    ]
    completed = subprocess.run(
        command,
        cwd=repo_root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        details = "\n".join(
            part.strip()
            for part in (completed.stdout, completed.stderr)
            if part and part.strip()
        )
        missing_module = re.search(r"ModuleNotFoundError: No module named '([^']+)'", details)
        if missing_module:
            raise RuntimeError(
                "Failed to initialize bot config because the target Nanobot environment is missing "
                f"Python package '{missing_module.group(1)}'."
            )
        raise RuntimeError(
            "Failed to initialize bot config via Nanobot CLI. "
            f"Command: {' '.join(command)}\n{details}"
        )
    sync_bot_runtime_config(config_path, workspace_path, web_port)


WEB_BIND_HOST_CHOICES = ("127.0.0.1", "0.0.0.0")


def get_web_bind_host(config_path: Path) -> str:
    if not config_path.exists():
        return WEB_BIND_HOST_CHOICES[0]
    config = json.loads(config_path.read_text(encoding="utf-8"))
    return config.get("gateway", {}).get("web", {}).get("host") or WEB_BIND_HOST_CHOICES[0]


def get_initial_web_password(config_path: Path) -> str:
    """The dashboard password nanobot generated on the bot's first start, while it is still in use.

    nanobot writes it to gateway.web.password with the "nanobot@" prefix and prints it once in the bot's
    console, which nobody sees for a bot adminbot starts. "" once the password was changed, or before
    the first start."""
    if not config_path.exists():
        return ""
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    web = (config.get("gateway") or {}).get("web") or {}
    password = web.get("password") if isinstance(web, dict) else ""
    return password if isinstance(password, str) and password.startswith("nanobot@") else ""


def update_web_bind_host(config_path: Path, host: str) -> None:
    if host not in WEB_BIND_HOST_CHOICES:
        raise ValueError(
            f"Unsupported bind host '{host}'. Allowed: {', '.join(WEB_BIND_HOST_CHOICES)}."
        )
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.setdefault("gateway", {})
    config["gateway"].setdefault("web", {})
    config["gateway"]["web"]["host"] = host
    atomic_write_json(config_path, config)


def sync_bot_runtime_config(config_path: Path, workspace_path: Path, web_port: int) -> None:
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.setdefault("gateway", {})
    config["gateway"].setdefault("web", {})
    config.setdefault("agents", {})
    config["agents"].setdefault("defaults", {})
    config["gateway"]["web"]["enabled"] = True
    config["gateway"]["web"]["port"] = int(web_port)
    child_web_host = os.environ.get("ADMINBOT_CHILD_WEB_HOST", "").strip()
    if child_web_host:
        config["gateway"]["web"]["host"] = child_web_host
    config["agents"]["defaults"]["workspace"] = str(workspace_path)
    atomic_write_json(config_path, config)
