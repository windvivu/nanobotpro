"""Workspace scope resolution for built-in file and exec tools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from nanobot.agent.skills import BUILTIN_SKILLS_DIR


@dataclass(frozen=True)
class WorkspaceScope:
    allowed_dir: Path | None
    extra_allowed_dirs: list[Path] | None
    extra_read_dirs: list[Path] | None
    exec_working_dir: str


def _sandbox_dir(workspace: Path, workspace_subdir: str) -> Path:
    sandbox_base = workspace / "sandbox"
    return sandbox_base / workspace_subdir if workspace_subdir else sandbox_base


def resolve_workspace_scope(
    *,
    workspace: Path,
    sandbox_mode: str,
    workspace_subdir: str = "",
    allowed_dirs: Sequence[str] | None = None,
    create_dirs: bool = True,
) -> WorkspaceScope:
    """Resolve current file-tool scope and exec working dir without changing behavior.

    create_dirs=False only computes the paths: the message tool checks attachments against the scope
    of a bot that has no file or exec tools, which needs no sandbox folder (custom)."""
    allowed_dir: Path | None
    if sandbox_mode == "workspace":
        allowed_dir = _sandbox_dir(workspace, workspace_subdir)
        if create_dirs:
            allowed_dir.mkdir(parents=True, exist_ok=True)
    elif sandbox_mode == "disabled":
        allowed_dir = workspace
    else:
        allowed_dir = None

    extra_allowed_dirs = [Path(d) for d in (allowed_dirs or [])] if sandbox_mode == "workspace" else None
    # Read-only extras: the bundled skills, and in "workspace" mode the workspace's own skills too, which
    # the prompt lists but which sit outside workspace/sandbox (custom)
    read_only_dirs = [BUILTIN_SKILLS_DIR, workspace / "skills"] if sandbox_mode == "workspace" else [BUILTIN_SKILLS_DIR]
    extra_read_dirs = (read_only_dirs + (extra_allowed_dirs or [])) if allowed_dir else extra_allowed_dirs

    if sandbox_mode not in ("unrestricted", "disabled"):
        exec_dir = _sandbox_dir(workspace, workspace_subdir)
        if create_dirs:
            exec_dir.mkdir(parents=True, exist_ok=True)
        exec_working_dir = str(exec_dir)
    else:
        exec_working_dir = str(workspace)

    return WorkspaceScope(
        allowed_dir=allowed_dir,
        extra_allowed_dirs=extra_allowed_dirs,
        extra_read_dirs=extra_read_dirs,
        exec_working_dir=exec_working_dir,
    )
