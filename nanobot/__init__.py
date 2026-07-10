"""
nanobot - A lightweight AI agent framework
"""

import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path


def _read_pyproject_version() -> str | None:
    """Read the source-tree version when package metadata is unavailable."""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.exists():
        return None
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return data.get("project", {}).get("version")


def _resolve_version() -> str:
    source_version = _read_pyproject_version()
    if source_version:
        return source_version
    try:
        return _pkg_version("nanobot")
    except PackageNotFoundError:
        try:
            return _pkg_version("nanobot-ai")
        except PackageNotFoundError:
            return "0.2.2"


__version__ = _resolve_version()
__logo__ = "🟢"

from nanobot.nanobot import Nanobot, RunResult  # noqa: E402

__all__ = ["Nanobot", "RunResult"]
