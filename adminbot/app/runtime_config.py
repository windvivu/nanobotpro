"""Per-bot config initialization and synchronization."""

from __future__ import annotations

import json
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
    config["agents"]["defaults"]["workspace"] = str(workspace_path)
    atomic_write_json(config_path, config)
