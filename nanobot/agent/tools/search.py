"""Search tools: grep and glob."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import time
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, TypeVar

from nanobot.agent.tools.filesystem import ListDirTool, _FsTool

_DEFAULT_HEAD_LIMIT = 250
T = TypeVar("T")
_TYPE_GLOB_MAP = {
    "py": ("*.py", "*.pyi"),
    "python": ("*.py", "*.pyi"),
    "js": ("*.js", "*.jsx", "*.mjs", "*.cjs"),
    "ts": ("*.ts", "*.tsx", "*.mts", "*.cts"),
    "tsx": ("*.tsx",),
    "jsx": ("*.jsx",),
    "json": ("*.json",),
    "md": ("*.md", "*.mdx"),
    "markdown": ("*.md", "*.mdx"),
    "go": ("*.go",),
    "rs": ("*.rs",),
    "rust": ("*.rs",),
    "java": ("*.java",),
    "sh": ("*.sh", "*.bash"),
    "yaml": ("*.yaml", "*.yml"),
    "yml": ("*.yaml", "*.yml"),
    "toml": ("*.toml",),
    "sql": ("*.sql",),
    "html": ("*.html", "*.htm"),
    "css": ("*.css", "*.scss", "*.sass"),
}


def _normalize_pattern(pattern: str) -> str:
    return pattern.strip().replace("\\", "/")


@lru_cache(maxsize=128)
def _glob_patterns(pattern: str) -> tuple[str, ...]:
    """Expand bounded brace alternatives before matching path segments."""
    pending = [_normalize_pattern(pattern)]
    expanded: list[str] = []
    while pending:
        current = pending.pop()
        braces = re.search(r"\{([^{}]*)\}", current)
        if braces is None:
            if "{" in current or "}" in current:
                raise ValueError("Invalid glob: unbalanced braces")
            expanded.append(current)
            continue
        options = braces[1].split(",")
        if len(options) < 2:
            raise ValueError("Invalid glob: braces require comma-separated alternatives")
        if len(expanded) + len(pending) + len(options) > 64:
            raise ValueError("Invalid glob: at most 64 brace alternatives are supported")
        pending.extend(
            current[:braces.start()] + option + current[braces.end():]
            for option in options
        )
    return tuple(expanded)


def _match_glob(rel_path: str, name: str, pattern: str) -> bool:
    return any(_match_path_glob(rel_path, name, item) for item in _glob_patterns(pattern))


def _match_path_glob(rel_path: str, name: str, normalized: str) -> bool:
    if not normalized:
        return False
    if "/" not in normalized:
        return fnmatch.fnmatch(name, normalized)
    pattern_parts = PurePosixPath(normalized).parts
    path_parts = PurePosixPath(rel_path).parts
    matched = [True] + [False] * len(path_parts)
    for part in pattern_parts:
        if part == "**":
            for index in range(1, len(matched)):
                matched[index] = matched[index] or matched[index - 1]
        else:
            matched = [False] + [
                matched[index] and fnmatch.fnmatchcase(path_part, part)
                for index, path_part in enumerate(path_parts)
            ]
    return matched[-1]


def _is_binary(raw: bytes) -> bool:
    if b"\x00" in raw:
        return True
    sample = raw[:4096]
    if not sample:
        return False
    non_text = sum(byte < 9 or 13 < byte < 32 for byte in sample)
    return (non_text / len(sample)) > 0.2


def _paginate(items: list[T], limit: int | None, offset: int) -> tuple[list[T], bool]:
    if limit is None:
        return items[offset:], False
    sliced = items[offset : offset + limit]
    truncated = len(items) > offset + limit
    return sliced, truncated


def _pagination_note(limit: int | None, offset: int, truncated: bool) -> str | None:
    if truncated:
        if limit is None:
            return f"(pagination: offset={offset})"
        return f"(pagination: limit={limit}, offset={offset})"
    if offset > 0:
        return f"(pagination: offset={offset})"
    return None


def _matches_type(name: str, file_type: str | None) -> bool:
    if not file_type:
        return True
    lowered = file_type.strip().lower()
    if not lowered:
        return True
    patterns = _TYPE_GLOB_MAP.get(lowered, (f"*.{lowered}",))
    return any(fnmatch.fnmatch(name.lower(), pattern.lower()) for pattern in patterns)


class _SearchTool(_FsTool):
    _IGNORE_DIRS = set(ListDirTool._IGNORE_DIRS) | {".worktrees", ".worktree", ".nanobot"}
    _MAX_SCAN_PATHS = 500_000
    _MAX_SCAN_SECONDS = 30.0
    _MAX_RESULT_CHARS = 128_000

    @classmethod
    def _ignore_directory(cls, name: str) -> bool:
        return name in cls._IGNORE_DIRS or name.startswith(".verify-")

    async def _checkpoint(self, started: float, scanned: int) -> None:
        if scanned % 128 == 0:
            await asyncio.sleep(0)
        if scanned > self._MAX_SCAN_PATHS:
            raise RuntimeError("scan path budget exceeded")
        if time.monotonic() - started > self._MAX_SCAN_SECONDS:
            raise RuntimeError("scan time budget exceeded")

    @staticmethod
    def _safe_scope_path(file_path: Path, root: Path) -> Path | None:
        try:
            resolved = file_path.resolve(strict=True)
            resolved.relative_to(root.resolve(strict=True))
        except (OSError, ValueError):
            return None
        return resolved

    @classmethod
    def _safe_read_path(cls, file_path: Path, root: Path) -> Path | None:
        resolved = cls._safe_scope_path(file_path, root)
        return resolved if resolved is not None and resolved.is_file() else None

    def _display_path(self, target: Path, root: Path) -> str:
        if self._workspace:
            try:
                return target.relative_to(self._workspace).as_posix()
            except ValueError:
                pass
        return target.relative_to(root).as_posix()

    def _iter_files(self, root: Path) -> Iterable[Path]:
        if root.is_file():
            yield root
            return

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not self._ignore_directory(d))
            current = Path(dirpath)
            for filename in sorted(filenames):
                yield current / filename

    def _iter_entries(
        self,
        root: Path,
        *,
        include_files: bool,
        include_dirs: bool,
    ) -> Iterable[Path]:
        if root.is_file():
            if include_files:
                yield root
            return

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not self._ignore_directory(d))
            current = Path(dirpath)
            if include_dirs:
                for dirname in dirnames:
                    yield current / dirname
            if include_files:
                for filename in sorted(filenames):
                    yield current / filename


class GlobTool(_SearchTool):
    """Find files matching a glob pattern."""

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return (
            "Find files matching a glob pattern (e.g. '*.py', 'tests/**/test_*.py'). "
            "Results are sorted by modification time (newest first). "
            "Skips .git, node_modules, __pycache__, and other noise directories."
        )

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match, e.g. '*.py' or 'tests/**/test_*.py'",
                    "minLength": 1,
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search from (default '.')",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Legacy alias for head_limit",
                    "minimum": 1,
                    "maximum": 1000,
                },
                "head_limit": {
                    "type": "integer",
                    "description": "Maximum number of matches to return (default 250)",
                    "minimum": 0,
                    "maximum": 1000,
                },
                "offset": {
                    "type": "integer",
                    "description": "Skip the first N matching entries before returning results",
                    "minimum": 0,
                    "maximum": 100000,
                },
                "entry_type": {
                    "type": "string",
                    "enum": ["files", "dirs", "both"],
                    "description": "Whether to match files, directories, or both (default files)",
                },
            },
            "required": ["pattern"],
        }

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        max_results: int | None = None,
        head_limit: int | None = None,
        offset: int = 0,
        entry_type: str = "files",
        **kwargs: Any,
    ) -> str:
        started = time.monotonic()
        scanned = 0
        try:
            root = self._resolve(path or ".")
            if not root.exists():
                return f"Error: Path not found: {path}"
            if not root.is_dir():
                return f"Error: Not a directory: {path}"

            if head_limit is not None:
                limit = None if head_limit == 0 else head_limit
            elif max_results is not None:
                limit = max_results
            else:
                limit = _DEFAULT_HEAD_LIMIT
            include_files = entry_type in {"files", "both"}
            include_dirs = entry_type in {"dirs", "both"}
            matches: list[tuple[str, float]] = []
            for entry in self._iter_entries(
                root,
                include_files=include_files,
                include_dirs=include_dirs,
            ):
                scanned += 1
                await self._checkpoint(started, scanned)
                if self._safe_scope_path(entry, root) is None:
                    continue
                rel_path = entry.relative_to(root).as_posix()
                if _match_glob(rel_path, entry.name, pattern):
                    display = self._display_path(entry, root)
                    if entry.is_dir():
                        display += "/"
                    try:
                        mtime = entry.stat().st_mtime
                    except OSError:
                        mtime = 0.0
                    matches.append((display, mtime))

            if not matches:
                return f"No paths matched pattern '{pattern}' in {path}"

            matches.sort(key=lambda item: (-item[1], item[0]))
            ordered = [name for name, _ in matches]
            paged, truncated = _paginate(ordered, limit, offset)
            result = "\n".join(paged)
            if len(result) > self._MAX_RESULT_CHARS:
                return "Error: Search result exceeds output budget; narrow the search path or pattern."
            if note := _pagination_note(limit, offset, truncated):
                result += f"\n\n{note}"
            return result
        except PermissionError as e:
            return f"Error: {e}"
        except RuntimeError as e:
            return f"Error: {e}; narrow the search path or pattern."
        except Exception as e:
            return f"Error finding files: {e}"


class GrepTool(_SearchTool):
    """Search file contents using a regex-like pattern."""
    _MAX_RESULT_CHARS = 128_000
    _MAX_FILE_BYTES = 2_000_000

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search file contents with a regex pattern. "
            "Default output_mode is files_with_matches (file paths only); "
            "use content mode for matching lines with context. "
            "Skips binary and files >2 MB. Supports glob/type filtering."
        )

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex or plain text pattern to search for",
                    "minLength": 1,
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in (default '.')",
                },
                "glob": {
                    "type": "string",
                    "description": "Optional file filter, e.g. '*.py' or 'tests/**/test_*.py'",
                },
                "type": {
                    "type": "string",
                    "description": "Optional file type shorthand, e.g. 'py', 'ts', 'md', 'json'",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case-insensitive search (default false)",
                },
                "fixed_strings": {
                    "type": "boolean",
                    "description": "Treat pattern as plain text instead of regex (default false)",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": (
                        "content: matching lines with optional context; "
                        "files_with_matches: only matching file paths; "
                        "count: matching line counts per file. "
                        "Default: files_with_matches"
                    ),
                },
                "context_before": {
                    "type": "integer",
                    "description": "Number of lines of context before each match",
                    "minimum": 0,
                    "maximum": 20,
                },
                "context_after": {
                    "type": "integer",
                    "description": "Number of lines of context after each match",
                    "minimum": 0,
                    "maximum": 20,
                },
                "max_matches": {
                    "type": "integer",
                    "description": (
                        "Legacy alias for head_limit in content mode"
                    ),
                    "minimum": 1,
                    "maximum": 1000,
                },
                "max_results": {
                    "type": "integer",
                    "description": (
                        "Legacy alias for head_limit in files_with_matches or count mode"
                    ),
                    "minimum": 1,
                    "maximum": 1000,
                },
                "head_limit": {
                    "type": "integer",
                    "description": (
                        "Maximum number of results to return. In content mode this limits "
                        "matching line blocks; in other modes it limits file entries. "
                        "Default 250"
                    ),
                    "minimum": 0,
                    "maximum": 1000,
                },
                "offset": {
                    "type": "integer",
                    "description": "Skip the first N results before applying head_limit",
                    "minimum": 0,
                    "maximum": 100000,
                },
            },
            "required": ["pattern"],
        }

    @staticmethod
    def _format_block(
        display_path: str,
        lines: list[str],
        match_line: int,
        before: int,
        after: int,
    ) -> str:
        start = max(1, match_line - before)
        end = min(len(lines), match_line + after)
        block = [f"{display_path}:{match_line}"]
        for line_no in range(start, end + 1):
            marker = ">" if line_no == match_line else " "
            block.append(f"{marker} {line_no}| {lines[line_no - 1]}")
        return "\n".join(block)

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        type: str | None = None,
        case_insensitive: bool = False,
        fixed_strings: bool = False,
        output_mode: str = "files_with_matches",
        context_before: int = 0,
        context_after: int = 0,
        max_matches: int | None = None,
        max_results: int | None = None,
        head_limit: int | None = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> str:
        started = time.monotonic()
        scanned = 0
        try:
            target = self._resolve(path or ".")
            if not target.exists():
                return f"Error: Path not found: {path}"
            if not (target.is_dir() or target.is_file()):
                return f"Error: Unsupported path: {path}"

            flags = re.IGNORECASE if case_insensitive else 0
            try:
                needle = re.escape(pattern) if fixed_strings else pattern
                regex = re.compile(needle, flags)
            except re.error as e:
                return f"Error: invalid regex pattern: {e}"

            if head_limit is not None:
                limit = None if head_limit == 0 else head_limit
            elif output_mode == "content" and max_matches is not None:
                limit = max_matches
            elif output_mode != "content" and max_results is not None:
                limit = max_results
            else:
                limit = _DEFAULT_HEAD_LIMIT
            blocks: list[str] = []
            result_chars = 0
            seen_content_matches = 0
            truncated = False
            size_truncated = False
            skipped_binary = 0
            skipped_large = 0
            skipped_scope = 0
            matching_files: list[str] = []
            counts: dict[str, int] = {}
            file_mtimes: dict[str, float] = {}
            root = target if target.is_dir() else target.parent

            for file_path in self._iter_files(target):
                scanned += 1
                await self._checkpoint(started, scanned)
                rel_path = file_path.relative_to(root).as_posix()
                if glob and not _match_glob(rel_path, file_path.name, glob):
                    continue
                if not _matches_type(file_path.name, type):
                    continue

                safe_path = self._safe_read_path(file_path, root)
                if safe_path is None:
                    skipped_scope += 1
                    continue
                raw = safe_path.read_bytes()
                if len(raw) > self._MAX_FILE_BYTES:
                    skipped_large += 1
                    continue
                if _is_binary(raw):
                    skipped_binary += 1
                    continue
                try:
                    mtime = safe_path.stat().st_mtime
                except OSError:
                    mtime = 0.0
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    skipped_binary += 1
                    continue

                lines = content.splitlines()
                display_path = self._display_path(file_path, root)
                file_had_match = False
                for idx, line in enumerate(lines, start=1):
                    if idx % 256 == 0:
                        await self._checkpoint(started, scanned)
                    if not regex.search(line):
                        continue
                    file_had_match = True

                    if output_mode == "count":
                        counts[display_path] = counts.get(display_path, 0) + 1
                        continue
                    if output_mode == "files_with_matches":
                        if display_path not in matching_files:
                            matching_files.append(display_path)
                            file_mtimes[display_path] = mtime
                        break

                    seen_content_matches += 1
                    if seen_content_matches <= offset:
                        continue
                    if limit is not None and len(blocks) >= limit:
                        truncated = True
                        break
                    block = self._format_block(
                        display_path,
                        lines,
                        idx,
                        context_before,
                        context_after,
                    )
                    extra_sep = 2 if blocks else 0
                    if result_chars + extra_sep + len(block) > self._MAX_RESULT_CHARS:
                        size_truncated = True
                        break
                    blocks.append(block)
                    result_chars += extra_sep + len(block)
                if output_mode == "count" and file_had_match:
                    if display_path not in matching_files:
                        matching_files.append(display_path)
                        file_mtimes[display_path] = mtime
                if output_mode in {"count", "files_with_matches"} and file_had_match:
                    continue
                if truncated or size_truncated:
                    break

            if output_mode == "files_with_matches":
                if not matching_files:
                    result = f"No matches found for pattern '{pattern}' in {path}"
                else:
                    ordered_files = sorted(
                        matching_files,
                        key=lambda name: (-file_mtimes.get(name, 0.0), name),
                    )
                    paged, truncated = _paginate(ordered_files, limit, offset)
                    result = "\n".join(paged)
            elif output_mode == "count":
                if not counts:
                    result = f"No matches found for pattern '{pattern}' in {path}"
                else:
                    ordered_files = sorted(
                        matching_files,
                        key=lambda name: (-file_mtimes.get(name, 0.0), name),
                    )
                    ordered, truncated = _paginate(ordered_files, limit, offset)
                    lines = [f"{name}: {counts[name]}" for name in ordered]
                    result = "\n".join(lines)
            else:
                if not blocks:
                    result = f"No matches found for pattern '{pattern}' in {path}"
                else:
                    result = "\n\n".join(blocks)

            notes: list[str] = []
            if output_mode == "content" and truncated:
                notes.append(
                    f"(pagination: limit={limit}, offset={offset})"
                )
            elif output_mode == "content" and size_truncated:
                notes.append("(output truncated due to size)")
            elif truncated and output_mode in {"count", "files_with_matches"}:
                notes.append(
                    f"(pagination: limit={limit}, offset={offset})"
                )
            elif output_mode in {"count", "files_with_matches"} and offset > 0:
                notes.append(f"(pagination: offset={offset})")
            elif output_mode == "content" and offset > 0 and blocks:
                notes.append(f"(pagination: offset={offset})")
            if skipped_binary:
                notes.append(f"(skipped {skipped_binary} binary/unreadable files)")
            if skipped_large:
                notes.append(f"(skipped {skipped_large} large files)")
            if skipped_scope:
                notes.append(f"(skipped {skipped_scope} files outside the search root)")
            if output_mode == "count" and counts:
                notes.append(
                    f"(total matches: {sum(counts.values())} in {len(counts)} files)"
                )
            if notes:
                result += "\n\n" + "\n".join(notes)
            if len(result) > self._MAX_RESULT_CHARS:
                return "Error: Search result exceeds output budget; narrow the search path or pattern."
            return result
        except PermissionError as e:
            return f"Error: {e}"
        except RuntimeError as e:
            return f"Error: {e}; narrow the search path or pattern."
        except Exception as e:
            return f"Error searching files: {e}"
