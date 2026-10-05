"""Bounded graph snapshots derived from active immutable facts."""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Iterable

from nanobot.brain_memory.config import BrainMemoryConfig
from nanobot.brain_memory.errors import DisabledError, FormatError
from nanobot.brain_memory.schemas import (
    Citation,
    Fact,
    GraphEdge,
    GraphNode,
    GraphSnapshot,
    content_hash,
)
from nanobot.brain_memory.store import BrainStore, parse_markdown

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}-{hashlib.sha256(value.casefold().strip().encode('utf-8')).hexdigest()[:24]}"


@dataclass(frozen=True)
class GraphFilters:
    topic: str | None = None
    status: str = "active"
    min_confidence: float = 0.0
    limit: int | None = None
    query: str | None = None


_CACHE_MAX_ENTRIES = 32
_CACHE_LOCK = RLock()
_SNAPSHOT_CACHE: OrderedDict[tuple, tuple[str, GraphSnapshot]] = OrderedDict()
_SIGNATURE_CACHE: dict[str, tuple[int, int, str]] = {}


class BrainGraph:
    """Build a response-safe snapshot without exposing arbitrary filesystem paths."""

    def __init__(self, store: BrainStore, config: BrainMemoryConfig | None = None):
        self.store = store
        self.config = config or store.config

    def _facts(self) -> Iterable[tuple[str, bytes, Fact]]:
        facts_dir = self.store.root / "facts"
        if not facts_dir.is_dir():
            return
        for path in sorted(facts_dir.glob("*.md")):
            try:
                ref = path.relative_to(self.store.root).as_posix()
                if path.is_symlink() or self.store.path(ref) != path.resolve():
                    continue
                raw = self.store.read_bytes(ref)
                metadata, body, first_line = parse_markdown(raw.decode("utf-8"))
                fact = Fact.model_validate({**metadata, "body": body})
            except (OSError, UnicodeError, ValueError, FormatError):
                raise FormatError(f"Invalid graph fact: {path.name}")
            yield ref, raw, fact

    def _file_signature(self) -> str:
        """Return a cheap invalidation token without reading fact bodies.

        Facts are append-only and written with atomic rename, so a directory
        timestamp change is enough to know when the per-file signature needs
        rebuilding. This keeps repeated cached graph reads off the 600-file
        stat path while still invalidating after normal fact writes.
        """
        facts_dir = self.store.root / "facts"
        cache_key = str(self.store.root)
        try:
            directory_stat = facts_dir.stat()
        except OSError:
            directory_stat = None
        if directory_stat is not None:
            directory_token = (directory_stat.st_mtime_ns, directory_stat.st_ctime_ns)
            with _CACHE_LOCK:
                cached = _SIGNATURE_CACHE.get(cache_key)
                if cached and cached[:2] == directory_token:
                    return cached[2]
        entries: list[tuple[str, int, int]] = []
        if facts_dir.is_dir():
            for path in sorted(facts_dir.glob("*.md")):
                try:
                    ref = path.relative_to(self.store.root).as_posix()
                    if path.is_symlink() or self.store.path(ref) != path.resolve():
                        continue
                    stat = path.stat()
                    entries.append((ref, stat.st_size, stat.st_mtime_ns))
                except (OSError, ValueError, FormatError):
                    # Let the regular parser produce the user-visible format
                    # error; an invalid entry must not poison another cache key.
                    entries.append((str(path), -1, -1))
        signature = content_hash(json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode())
        if directory_stat is not None:
            with _CACHE_LOCK:
                _SIGNATURE_CACHE[cache_key] = (*directory_token, signature)
        return signature

    def _cache_key(self, filters: GraphFilters) -> tuple:
        return (
            str(self.store.root), filters.topic.casefold() if filters.topic else None,
            filters.status, filters.min_confidence, filters.limit,
            filters.query.casefold().strip() if filters.query else None,
            self.config.graph_max_nodes, self.config.graph_max_edges,
        )

    @staticmethod
    def _superseded_ids(facts: Iterable[Fact]) -> set[str]:
        return {
            old_id
            for fact in facts if fact.status == "active"
            for old_id in fact.supersedes
        }

    def build(self, filters: GraphFilters | None = None, *, refresh: bool = False) -> GraphSnapshot:
        if not self.config.enabled or not self.config.graph_enabled:
            raise DisabledError("Brain Memory graph is disabled")
        filters = filters or GraphFilters()
        if not 0 <= filters.min_confidence <= 1:
            raise ValueError("min_confidence must be between 0 and 1")
        if filters.limit is not None and not 1 <= filters.limit <= self.config.graph_max_nodes:
            raise ValueError("graph limit exceeds configured node budget")
        cache_key = self._cache_key(filters)
        signature = self._file_signature()
        if not refresh:
            with _CACHE_LOCK:
                cached = _SNAPSHOT_CACHE.get(cache_key)
                if cached and cached[0] == signature:
                    _SNAPSHOT_CACHE.move_to_end(cache_key)
                    return cached[1]
        records = list(self._facts())
        facts = [fact for _, _, fact in records]
        superseded = self._superseded_ids(facts)
        search_query = filters.query.casefold().strip() if filters.query else ""
        selected: list[tuple[str, bytes, Fact]] = []
        for ref, raw, fact in records:
            effective_status = 'superseded' if fact.status == 'active' and fact.id in superseded else fact.status
            if effective_status != filters.status:
                continue
            if fact.confidence < filters.min_confidence:
                continue
            if filters.topic and fact.topic.casefold() != filters.topic.casefold():
                continue
            if search_query:
                searchable = " ".join((
                    fact.title, fact.body, fact.topic,
                    *fact.entities, *fact.tags,
                )).casefold()
                if search_query not in searchable:
                    continue
            selected.append((ref, raw, fact))
        node_limit = filters.limit or self.config.graph_max_nodes
        selected = selected[:node_limit]

        nodes: dict[str, GraphNode] = {}
        links: set[tuple[str, str, str]] = set()
        selected_ids = {fact.id for _, _, fact in selected}
        for ref, raw, fact in selected:
            metadata, body, first_line = parse_markdown(raw.decode("utf-8"))
            citation = Citation(
                source_ref=ref, source_hash=content_hash(raw),
                line_start=first_line, line_end=max(first_line, first_line + len(body.splitlines()) - 1),
            )
            nodes[fact.id] = GraphNode(
                id=fact.id, label=fact.title, kind="fact", citation=citation,
            )
            topic_id = _stable_id("topic", fact.topic)
            nodes.setdefault(topic_id, GraphNode(id=topic_id, label=fact.topic, kind="topic"))
            links.add((fact.id, topic_id, "topic"))
            for entity in fact.entities:
                entity_id = _stable_id("entity", entity)
                nodes.setdefault(entity_id, GraphNode(id=entity_id, label=entity, kind="entity"))
                links.add((fact.id, entity_id, "entity"))
            for target, _alias in _WIKILINK_RE.findall(body):
                target_id = target.strip()
                if target_id in selected_ids:
                    links.add((fact.id, target_id, "wikilink"))

        # Reserve up to one third for topic/entity nodes so clipping does not
        # leave only disconnected facts. Never return edges to clipped nodes.
        auxiliary = sorted((node for node in nodes.values() if node.kind != 'fact'), key=lambda node: node.id)
        auxiliary = auxiliary[: node_limit // 3]
        fact_nodes = sorted((node for node in nodes.values() if node.kind == 'fact'), key=lambda node: node.id)
        ordered_nodes = tuple(fact_nodes[: node_limit - len(auxiliary)] + auxiliary)
        retained_ids = {node.id for node in ordered_nodes}
        retained_links = [edge for edge in sorted(links) if edge[0] in retained_ids and edge[1] in retained_ids]
        ordered_links = tuple(
            GraphEdge(source=source, target=target, kind=kind)
            for source, target, kind in retained_links[: self.config.graph_max_edges]
        )
        generation = content_hash("\n".join(sorted(content_hash(raw) for _, raw, _ in records)).encode())
        snapshot = GraphSnapshot(generation=generation, nodes=ordered_nodes, links=ordered_links)
        with _CACHE_LOCK:
            _SNAPSHOT_CACHE[cache_key] = (signature, snapshot)
            _SNAPSHOT_CACHE.move_to_end(cache_key)
            while len(_SNAPSHOT_CACHE) > _CACHE_MAX_ENTRIES:
                _SNAPSHOT_CACHE.popitem(last=False)
        return snapshot
