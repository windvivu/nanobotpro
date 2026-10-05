"""Portable workspace references; no user-controlled absolute filesystem paths."""

from pathlib import Path, PureWindowsPath

from nanobot.brain_memory.errors import ScopeError


def validate_ref(value: str) -> str:
    """Require canonical relative POSIX refs on both Windows and POSIX hosts."""
    if not value or len(value) > 512 or value != value.strip():
        raise ScopeError("Invalid relative reference")
    if value.startswith("/") or PureWindowsPath(value).drive or "\\" in value:
        raise ScopeError("References must be relative POSIX paths")
    for part in value.split("/"):
        if part in {"", ".", ".."} or part.endswith((" ", ".")):
            raise ScopeError("Non-canonical reference")
        if any(ord(c) < 32 or c in '<>:"|?*' for c in part):
            raise ScopeError("Invalid reference component")
        if part.split(".")[0].upper() in {
            "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }:
            raise ScopeError("Reserved reference component")
    return value


def resolve_ref(root: Path, ref: str) -> Path:
    """Reject symlink/junction components, including dangling links, before I/O.

    The root is trusted configuration. Files below it may not redirect I/O.
    This is not a sandbox against a process concurrently replacing directories.
    """
    validate_ref(ref)
    root = root.resolve()
    candidate = root
    for part in ref.split("/"):
        candidate /= part
        if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
            raise ScopeError("Linked paths are not permitted in Brain Memory")
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (ValueError, OSError, RuntimeError) as exc:
        raise ScopeError("Reference escapes its root") from exc
    return resolved
