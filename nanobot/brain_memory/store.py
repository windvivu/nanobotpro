"""Confined, locked, atomic Markdown storage. No learning or indexing side effects."""

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import yaml
from filelock import FileLock

from nanobot.brain_memory.config import BrainMemoryConfig
from nanobot.brain_memory.errors import ConflictError, DisabledError, FormatError
from nanobot.brain_memory.paths import resolve_ref
from nanobot.brain_memory.schemas import Fact


class _UniqueLoader(yaml.SafeLoader):
    """Reject duplicate frontmatter keys rather than silently overwriting them."""

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise FormatError("Frontmatter keys must be unique strings")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def render_markdown(metadata: dict, body: str) -> str:
    # JSON scalars/collections are a YAML subset. One key per line keeps mapping
    # stable, safely quotes delimiters, and remains readable in Markdown editors.
    header = "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False, allow_nan=False)}"
        for key, value in sorted(metadata.items())
    )
    return f"---\n{header}\n---\n{body}"


def parse_markdown(text: str) -> tuple[dict, str, int]:
    """Return metadata, body and its 1-based first line (including blank lines)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise FormatError("Missing frontmatter")
    for index, line in enumerate(lines[1:513], start=1):
        if line.strip() == "---":
            header = "".join(lines[1:index])
            if len(header.encode("utf-8")) > 65536:
                raise FormatError("Frontmatter exceeds budget")
            try:
                if any(isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken))
                       for token in yaml.scan(header)):
                    raise FormatError("YAML anchors/aliases are not supported")
                metadata = yaml.load(header, Loader=_UniqueLoader)
            except (yaml.YAMLError, RecursionError) as exc:
                raise FormatError("Invalid frontmatter") from exc
            if not isinstance(metadata, dict):
                raise FormatError("Frontmatter must be a mapping")
            return metadata, "".join(lines[index + 1:]), index + 2
    raise FormatError("Missing or oversized frontmatter delimiter")


class BrainStore:
    """Constructors are read-only. Mutations require explicit enabled config.

    The lock serializes cooperating writers. An external process with write
    access to the workspace is outside this confinement boundary.
    """

    def __init__(self, workspace: Path, config: BrainMemoryConfig):
        self.workspace = workspace.expanduser().resolve()
        self.config = config.model_copy(deep=True)
        self.root = resolve_ref(self.workspace, self.config.root)

    def path(self, ref: str) -> Path:
        # Re-check the configured root each time, including after construction.
        if resolve_ref(self.workspace, self.config.root) != self.root:
            raise FormatError("Brain root changed")
        return resolve_ref(self.root, ref)

    @contextmanager
    def locked(self) -> Iterator[None]:
        if not self.config.enabled:
            raise DisabledError("Brain Memory is disabled")
        self.path(".write.lock")  # validate before creating anything
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(self.path(".write.lock"), timeout=self.config.lock_timeout_seconds):
            yield

    def read_bytes(self, ref: str) -> bytes:
        path = self.path(ref)
        if not path.is_file():
            raise FormatError("Source is not a regular file")
        with path.open("rb") as stream:
            raw = stream.read(self.config.max_document_bytes + 1)
        if len(raw) > self.config.max_document_bytes:
            raise FormatError("Document exceeds byte budget")
        return raw

    def _write_locked(self, ref: str, raw: bytes, *, replace: bool = False) -> bool:
        """Internal: caller holds locked(). Identical retries are no-ops."""
        if len(raw) > self.config.max_document_bytes:
            raise FormatError("Document exceeds byte budget")
        path = self.path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path = self.path(ref)
        if path.exists():
            if self.read_bytes(ref) == raw:
                return False
            if not replace:
                raise ConflictError("Immutable record already exists")
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".brain-", delete=False) as f:
                temp_path = Path(f.name)
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.path(ref))
            # Best effort directory sync where supported (not available on Windows).
            if os.name != "nt":
                fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return True

    def append_fact(self, fact: Fact) -> bool:
        """Low-level trusted writer; promotion policy belongs to promoter.py."""
        if len(fact.body) > self.config.max_fact_chars:
            raise FormatError("Fact exceeds character budget")
        metadata = fact.model_dump(mode="json", exclude={"body"})
        raw = render_markdown(metadata, fact.body).encode("utf-8")
        with self.locked():
            return self._write_locked(f"facts/{fact.id}.md", raw)

    def read_fact(self, fact_id: str) -> Fact:
        # Validate ID independently of path handling; no arbitrary path API.
        from pydantic import TypeAdapter

        from nanobot.brain_memory.schemas import FactId

        TypeAdapter(FactId).validate_python(fact_id)
        metadata, body, _ = parse_markdown(self.read_bytes(f"facts/{fact_id}.md").decode("utf-8"))
        return Fact.model_validate({**metadata, "body": body})
