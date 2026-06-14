"""Path helpers for Adminbot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    repo_root: Path
    adminbot_root: Path
    runtime_root: Path
    registry_path: Path
    instances_dir: Path
    logs_dir: Path
    run_dir: Path
    python_executable: Path


def resolve_repo_root() -> Path:
    env_root = os.environ.get("ADMINBOT_REPO_ROOT", "").strip()
    if env_root:
        candidate = Path(env_root).expanduser().resolve()
    else:
        candidate = Path(__file__).resolve().parents[2]

    expected_adminbot = candidate / "adminbot"
    expected_package = expected_adminbot / "app" / "__init__.py"
    if not expected_adminbot.is_dir() or not expected_package.is_file():
        raise RuntimeError(
            "Unable to resolve repo root for adminbot. "
            "Expected a valid adminbot package under the resolved root. "
            "Set ADMINBOT_REPO_ROOT if this repo uses a different layout."
        )
    return candidate


def resolve_python_executable(repo_root: Path) -> Path:
    candidates = [
        repo_root / "venv" / "Scripts" / "python.exe",
        repo_root / ".venv" / "Scripts" / "python.exe",
        repo_root / "venv" / "bin" / "python",
        repo_root / ".venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError(
        "Unable to find a repo-local Python executable. "
        "Expected one under venv/ or .venv/."
    )


def build_runtime_paths(repo_root: Path | None = None) -> RuntimePaths:
    resolved_root = repo_root or resolve_repo_root()
    adminbot_root = resolved_root / "adminbot"
    runtime_root = resolved_root / ".adminbot"
    return RuntimePaths(
        repo_root=resolved_root,
        adminbot_root=adminbot_root,
        runtime_root=runtime_root,
        registry_path=runtime_root / "bots.json",
        instances_dir=runtime_root / "instances",
        logs_dir=runtime_root / "logs",
        run_dir=runtime_root / "run",
        python_executable=resolve_python_executable(resolved_root),
    )


def ensure_runtime_dirs(paths: RuntimePaths) -> None:
    paths.runtime_root.mkdir(parents=True, exist_ok=True)
    paths.instances_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
