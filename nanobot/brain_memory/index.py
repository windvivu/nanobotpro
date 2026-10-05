"""Rebuildable SQLite FTS5 index for immutable Markdown facts."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable

from nanobot.brain_memory.errors import DisabledError, FormatError
from nanobot.brain_memory.schemas import Citation, Fact, RetrievedChunk, content_hash
from nanobot.brain_memory.store import BrainStore, parse_markdown

_TOKEN_RE = re.compile(r"[\wÀ-ỹ]+", re.UNICODE)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]+)?\]\]")
_CHUNK_LINES = 80
_CHUNK_CHARS = 4000
_INDEX_SCHEMA_VERSION = "3"


@dataclass(frozen=True)
class IndexStats:
    fact_count: int
    chunk_count: int
    generation: str


def _tokens(text: str) -> list[str]:
    return [token.casefold() for token in _TOKEN_RE.findall(text) if len(token) <= 80]


def _fts_query(query: str) -> str:
    # Treat user input as terms, never as raw FTS5 syntax. Prefix matching keeps
    # Vietnamese and partial words useful while bounded token count limits work.
    terms = list(dict.fromkeys(_tokens(query)))[:32]
    return " OR ".join(f'"{term.replace(chr(34), "")}"*' for term in terms)


def _chunk_lines(body: str) -> Iterable[tuple[int, int, str]]:
    lines = body.splitlines()
    start = 0
    while start < len(lines):
        end = min(start + _CHUNK_LINES, len(lines))
        while end > start + 1 and len("\n".join(lines[start:end])) > _CHUNK_CHARS:
            end -= 1
        text = "\n".join(lines[start:end]).strip()
        if text:
            yield start + 1, end, text
        start = end


class BrainIndex:
    """SQLite cache whose contents can always be reconstructed from facts."""

    def __init__(self, store: BrainStore):
        self.store = store
        self.config = store.config
        self.db_path = store.root / "index.sqlite3"
        self._ensure_fts5()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.store.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=self.config.lock_timeout_seconds)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _ensure_fts5() -> None:
        try:
            with sqlite3.connect(":memory:") as conn:
                conn.execute("CREATE VIRTUAL TABLE brain_fts_check USING fts5(text)")
        except sqlite3.Error as exc:
            raise FormatError("SQLite FTS5 is unavailable") from exc

    def _ensure_schema(self) -> None:
        if not self.config.enabled:
            return
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS brain_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS facts (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    source_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    type TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    entities TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    supersedes TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    line_start INTEGER NOT NULL,
                    line_end INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    source_hash TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    text,
                    title,
                    topic,
                    entities,
                    tags
                );
                CREATE TABLE IF NOT EXISTS links (
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    link_type TEXT NOT NULL,
                    PRIMARY KEY(source_id, target_id, link_type)
                );
                CREATE INDEX IF NOT EXISTS links_target_idx
                    ON links(target_id, link_type);
                CREATE INDEX IF NOT EXISTS links_target_cover_idx
                    ON links(target_id, link_type, source_id);
                CREATE INDEX IF NOT EXISTS links_source_idx
                    ON links(source_id, link_type);
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO brain_meta VALUES ('schema_version', ?)",
                (_INDEX_SCHEMA_VERSION,),
            )
            version = conn.execute(
                "SELECT value FROM brain_meta WHERE key='schema_version'"
            ).fetchone()[0]
            if version in {"1", "2"}:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(facts)")}
                if version == "1" and "supersedes" not in columns:
                    conn.execute("ALTER TABLE facts ADD COLUMN supersedes TEXT NOT NULL DEFAULT ''")
                # v2 created the links table but never populated it. Backfill
                # once so reverse wikilink retrieval also works on existing
                # indexes after upgrading to v3.
                conn.execute("DELETE FROM links")
                for row in list(conn.execute("SELECT fact_id, text FROM chunks")):
                    for target in _WIKILINK_RE.findall(row["text"]):
                        conn.execute(
                            "INSERT OR IGNORE INTO links VALUES (?, ?, 'wikilink')",
                            (row["fact_id"], target.strip()),
                        )
                conn.execute(
                    "UPDATE brain_meta SET value = ? WHERE key = 'schema_version'",
                    (_INDEX_SCHEMA_VERSION,),
                )
                version = _INDEX_SCHEMA_VERSION
            if version != _INDEX_SCHEMA_VERSION:
                raise FormatError(f"Unsupported Brain Memory index schema: {version}")
            conn.commit()

    def _iter_fact_files(self) -> Iterable[tuple[str, bytes]]:
        facts_dir = self.store.root / "facts"
        if not facts_dir.is_dir():
            return
        for path in sorted(facts_dir.glob("*.md")):
            try:
                relative = path.relative_to(self.store.root).as_posix()
                safe = self.store.path(relative)
                raw = self.store.read_bytes(relative)
            except (OSError, ValueError, FormatError):
                continue
            if safe != path.resolve() or path.is_symlink():
                continue
            yield relative, raw

    @staticmethod
    def _fact_rows(path: str, raw: bytes) -> tuple[Fact, list[tuple]]:
        try:
            metadata, body, first_body_line = parse_markdown(raw.decode("utf-8"))
            fact = Fact.model_validate({**metadata, "body": body})
        except (UnicodeError, ValueError, FormatError) as exc:
            raise FormatError(f"Invalid fact file: {path}") from exc
        source_hash = content_hash(raw)
        chunks = []
        for index, (line_start, line_end, text) in enumerate(_chunk_lines(body)):
            chunk_id = f"{fact.id}:{index}"
            chunks.append((
                chunk_id,
                fact.id,
                path,
                first_body_line + line_start - 1,
                first_body_line + line_end - 1,
                text,
                source_hash,
                fact.title,
                fact.topic,
                " ".join(fact.entities),
                " ".join(fact.tags),
            ))
        return fact, chunks

    def _clear(self, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM chunks_fts")
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM facts")
        conn.execute("DELETE FROM links")

    @staticmethod
    def _remove_fact(conn: sqlite3.Connection, fact_id: str) -> None:
        chunk_ids = [row[0] for row in conn.execute(
            "SELECT chunk_id FROM chunks WHERE fact_id = ?", (fact_id,)
        )]
        conn.executemany("DELETE FROM chunks_fts WHERE chunk_id = ?", ((item,) for item in chunk_ids))
        conn.execute("DELETE FROM chunks WHERE fact_id = ?", (fact_id,))
        conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        conn.execute("DELETE FROM links WHERE source_id = ? OR target_id = ?", (fact_id, fact_id))

    @staticmethod
    def _insert_fact(conn: sqlite3.Connection, path: str, raw: bytes) -> tuple[Fact, int]:
        fact, chunks = BrainIndex._fact_rows(path, raw)
        conn.execute(
            "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fact.id, path, content_hash(raw), fact.status, fact.type,
                fact.topic, json_tuple(fact.entities), json_tuple(fact.tags),
                " ".join(fact.supersedes), fact.confidence, fact.created_at,
            ),
        )
        for chunk in chunks:
            conn.execute("INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?)", chunk[:7])
            conn.execute(
                "INSERT INTO chunks_fts VALUES (?, ?, ?, ?, ?, ?)",
                (chunk[0], chunk[5], chunk[7], chunk[8], chunk[9], chunk[10]),
            )
            for target in _WIKILINK_RE.findall(chunk[5]):
                conn.execute(
                    "INSERT OR IGNORE INTO links VALUES (?, ?, 'wikilink')",
                    (fact.id, target.strip()),
                )
        return fact, len(chunks)

    @staticmethod
    def _refresh_generation(conn: sqlite3.Connection) -> str:
        hashes = [row[0] for row in conn.execute("SELECT source_hash FROM facts ORDER BY id")]
        generation = content_hash("\n".join(hashes).encode())
        conn.execute("INSERT OR REPLACE INTO brain_meta VALUES ('generation', ?)", (generation,))
        return generation

    def rebuild(self) -> IndexStats:
        if not self.config.enabled:
            raise DisabledError("Brain Memory is disabled")
        with self.store.locked():
            with self._connection() as conn:
                try:
                    conn.execute("BEGIN")
                    self._clear(conn)
                    fact_count = chunk_count = 0
                    for path, raw in self._iter_fact_files():
                        fact, chunk_count_for_fact = self._insert_fact(conn, path, raw)
                        fact_count += 1
                        chunk_count += chunk_count_for_fact
                    generation = self._refresh_generation(conn)
                    conn.execute("COMMIT")
                except Exception:
                    conn.rollback()
                    raise
        return IndexStats(fact_count, chunk_count, generation)

    def index_fact(self, fact: Fact) -> IndexStats:
        """Update one immutable fact without rebuilding unrelated records."""
        if not self.config.enabled:
            raise DisabledError("Brain Memory is disabled")
        ref = f"facts/{fact.id}.md"
        raw = self.store.read_bytes(ref)
        parsed, _ = self._fact_rows(ref, raw)
        if parsed.id != fact.id:
            raise FormatError("Fact file does not match requested ID")
        with self.store.locked():
            with self._connection() as conn:
                conn.execute("BEGIN")
                self._remove_fact(conn, fact.id)
                self._insert_fact(conn, ref, raw)
                generation = self._refresh_generation(conn)
                conn.execute("COMMIT")
                fact_count = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
                chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return IndexStats(fact_count, chunk_count, generation)

    def generation(self) -> str:
        if not self.config.enabled:
            return ""
        with self._connection() as conn:
            row = conn.execute("SELECT value FROM brain_meta WHERE key='generation'").fetchone()
        return str(row[0]) if row else ""

    def search(self, query: str, *, limit: int | None = None, token_budget: int | None = None) -> list[RetrievedChunk]:
        if not self.config.enabled or not self.config.retrieval_enabled:
            return []
        if len(query) > self.config.max_query_chars:
            raise ValueError("Memory query exceeds character budget")
        match = _fts_query(query)
        if not match:
            return []
        limit = min(limit or self.config.retrieval_limit, self.config.retrieval_limit)
        budget = min(token_budget or self.config.retrieval_token_budget, self.config.retrieval_token_budget)
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT c.chunk_id, c.fact_id, c.path, c.line_start, c.line_end,
                       c.text, c.source_hash, bm25(chunks_fts) AS rank
                FROM chunks_fts
                JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
                JOIN facts f ON f.id = c.fact_id
                WHERE chunks_fts MATCH ? AND f.status = 'active'
                  AND NOT EXISTS (
                      SELECT 1 FROM facts newer
                      WHERE newer.status = 'active'
                        AND instr(' ' || newer.supersedes || ' ', ' ' || f.id || ' ') > 0
                  )
                ORDER BY rank ASC, c.chunk_id ASC
                LIMIT ?
                """,
                (match, limit * 4),
            ).fetchall()
            # Relation expansion is deliberately done after direct FTS hits.
            # The metadata scan is bounded by the indexed fact count and keeps
            # related facts lower priority than an exact query match.
            # Only direct rows that can fit in the response are allowed to
            # seed relation expansion. This caps relation work at retrieval
            # limit even when the FTS over-fetch window is large.
            direct_ids = {row["fact_id"] for row in rows[:limit]}
            related_ids: list[str] = []
            if direct_ids and self.config.retrieval_relation_hops:
                eligible = conn.execute(
                    """
                    SELECT f.id, f.topic, f.entities
                    FROM facts f
                    WHERE f.status = 'active'
                      AND NOT EXISTS (
                          SELECT 1 FROM facts newer
                          WHERE newer.status = 'active'
                            AND instr(' ' || newer.supersedes || ' ', ' ' || f.id || ' ') > 0
                      )
                    """
                ).fetchall()
                metadata = {
                    row["id"]: (row["topic"], _decode_tuple(row["entities"]))
                    for row in eligible
                }
                direct_meta = [metadata[item] for item in direct_ids if item in metadata]
                if direct_meta:
                    direct_chunks = conn.execute(
                        "SELECT fact_id, text FROM chunks WHERE fact_id IN ({})".format(
                            ",".join("?" * len(direct_ids))
                        ), tuple(direct_ids),
                    ).fetchall()
                    direct_wikilinks = {
                        target.strip()
                        for row in direct_chunks
                        for target in _WIKILINK_RE.findall(row["text"])
                    }
                    candidate_scores: dict[str, int] = {}
                    for fact_id, (topic, entities) in metadata.items():
                        if fact_id in direct_ids:
                            continue
                        score = 0
                        for direct_topic, direct_entities in direct_meta:
                            if topic.casefold() == direct_topic.casefold():
                                score += 2
                            if entities and direct_entities:
                                score += 3 * len(entities & direct_entities)
                        if fact_id in direct_wikilinks:
                            score += 4
                        if score:
                            candidate_scores[fact_id] = score

                    # The indexed links table keeps reverse wikilink lookup
                    # cheap and bounded; a target query can discover sources
                    # that point at it without scanning chunk bodies.
                    reverse_rows = conn.execute(
                        """
                        SELECT DISTINCT source_id
                        FROM links
                        WHERE target_id IN ({}) AND link_type = 'wikilink'
                        """.format(",".join("?" * len(direct_ids))),
                        tuple(direct_ids),
                    ).fetchall()
                    for row in reverse_rows:
                        source_id = row["source_id"]
                        if source_id not in direct_ids and source_id in metadata:
                            candidate_scores[source_id] = candidate_scores.get(source_id, 0) + 4

                    related_ids = [
                        fact_id for fact_id, _score in sorted(
                            candidate_scores.items(), key=lambda item: (-item[1], item[0])
                        )[:self.config.retrieval_relation_limit]
                    ]

            related_rows = []
            if related_ids:
                related_rows = conn.execute(
                    """
                    SELECT c.chunk_id, c.fact_id, c.path, c.line_start, c.line_end,
                           c.text, c.source_hash
                    FROM chunks c
                    JOIN facts f ON f.id = c.fact_id
                    WHERE c.fact_id IN ({}) AND f.status = 'active'
                      AND NOT EXISTS (
                          SELECT 1 FROM facts newer
                          WHERE newer.status = 'active'
                            AND instr(' ' || newer.supersedes || ' ', ' ' || f.id || ' ') > 0
                      )
                    ORDER BY c.fact_id ASC, c.chunk_id ASC
                    """.format(",".join("?" * len(related_ids))),
                    tuple(related_ids),
                ).fetchall()
                relation_order = {fact_id: index for index, fact_id in enumerate(related_ids)}
                related_rows.sort(key=lambda row: (relation_order[row["fact_id"]], row["chunk_id"]))

        # Keep one representative chunk per related fact. Direct FTS chunks
        # retain their ranking and always precede relation-expanded context.
        selected_rows = list(rows)
        seen_related: set[str] = set()
        for row in related_rows:
            if row["fact_id"] not in seen_related:
                selected_rows.append({**dict(row), "rank": 0.0})
                seen_related.add(row["fact_id"])
        results: list[RetrievedChunk] = []
        used = 0
        for row in selected_rows:
            if len(results) >= limit:
                break
            words = row["text"].split()
            if used + len(words) > budget:
                # Preserve the direct hit even when one indexed chunk is
                # larger than the whole budget. A bounded excerpt is safer
                # than returning only a lower-priority related fact.
                if not results and row["fact_id"] in direct_ids and budget:
                    clipped = " ".join(words[:budget])
                    results.append(RetrievedChunk(
                        chunk_id=row["chunk_id"], fact_id=row["fact_id"],
                        text=clipped, score=float(-row["rank"]),
                        citation=Citation(
                            source_ref=row["path"], source_hash=row["source_hash"],
                            line_start=row["line_start"], line_end=row["line_end"],
                        ),
                    ))
                    used = budget
                continue
            used += len(words)
            results.append(RetrievedChunk(
                chunk_id=row["chunk_id"], fact_id=row["fact_id"],
                text=row["text"], score=float(-row["rank"]),
                citation=Citation(
                    source_ref=row["path"], source_hash=row["source_hash"],
                    line_start=row["line_start"], line_end=row["line_end"],
                ),
            ))
        return results


def json_tuple(values: tuple[str, ...]) -> str:
    # Preserve entity/tag labels exactly.  Splitting a space-joined value would
    # make "machine learning" collide with unrelated labels sharing "machine".
    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def _decode_tuple(value: str) -> set[str]:
    """Read current JSON metadata and tolerate pre-hardening indexes."""
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return set(value.split())
    return {item for item in parsed if isinstance(item, str)} if isinstance(parsed, list) else set()
