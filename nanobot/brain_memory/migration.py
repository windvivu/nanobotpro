"""Explicit local dry-run imports. Source files and legacy memory stay untouched."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from nanobot.brain_memory.errors import FormatError, ScopeError
from nanobot.brain_memory.paths import resolve_ref
from nanobot.brain_memory.schemas import canonical_hash, content_hash
from nanobot.brain_memory.store import BrainStore, parse_markdown, render_markdown


@dataclass(frozen=True)
class MigrationResult:
    job_id: str
    source_ref: str
    source_hash: str
    snapshot_ref: str
    staged: bool
    created: bool


def migration_batch_id(results: list[MigrationResult]) -> str:
    """Return a stable ID for one explicit multi-source migration action."""
    return "batch-" + canonical_hash({
        "schema_version": 1,
        "jobs": sorted((result.job_id, result.source_ref, result.source_hash) for result in results),
    })[7:]


def current_promotion_sequence(store: BrainStore) -> int:
    ref = ".migration/promotion-sequence.json"
    if not store.path(ref).exists():
        return 0
    try:
        record = json.loads(store.read_bytes(ref).decode("utf-8"))
        value = record.get("value")
        if record.get("schema_version") != 1 or type(value) is not int or value < 0:
            raise ValueError("invalid promotion sequence")
        return value
    except (UnicodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
        raise FormatError("Promotion sequence is invalid") from exc


def write_migration_batch(
    store: BrainStore,
    results: list[MigrationResult],
    batch_id: str,
    learning_job_ids: list[str] | None = None,
) -> dict:
    """Create/update the durable metadata used by the guarded undo action."""
    ref = f".migration/batches/{batch_id}.json"
    now = datetime.now(timezone.utc).isoformat()
    with store.locked():
        record = {
            "schema_version": 1,
            "batch_id": batch_id,
            "created_at": now,
            "source_refs": sorted({result.source_ref for result in results}),
            "import_job_ids": [result.job_id for result in results],
            "learning_job_ids": list(learning_job_ids or []),
            "promotion_sequence_before": current_promotion_sequence(store),
            "undone": False,
        }
        if store.path(ref).exists():
            try:
                old = json.loads(store.read_bytes(ref).decode("utf-8"))
                if isinstance(old, dict):
                    record["created_at"] = old.get("created_at", record["created_at"])
                    record["promotion_sequence_before"] = old.get(
                        "promotion_sequence_before", record["promotion_sequence_before"]
                    )
                    record["undone"] = bool(old.get("undone", False))
                    if not learning_job_ids:
                        record["learning_job_ids"] = list(old.get("learning_job_ids", []))
            except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                raise FormatError("Migration batch is invalid")
        store._write_locked(
            ref,
            (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
            replace=True,
        )
    return record


def _conversation(raw: bytes) -> tuple[str, list[dict], str]:
    """Keep source JSONL line -> snapshot body line mapping; omit non-text payloads."""
    body_lines: list[str] = []
    mapping: list[dict] = []
    session_key = ""
    try:
        for line_no, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            message = json.loads(line)
            if not isinstance(message, dict):
                raise FormatError("Session record must be an object")
            if message.get("_type") == "metadata":
                session_key = str(message.get("key", ""))[:200]
                continue
            role = message.get("role")
            if role not in {"user", "assistant", "tool", "system"}:
                raise FormatError("Unknown session role")
            content = message.get("content")
            if content is None:
                content = "[non-text message omitted]"
            elif isinstance(content, list):
                content = "\n".join(
                    block["text"] if isinstance(block, dict) and isinstance(block.get("text"), str)
                    else "[non-text block omitted]" for block in content
                )
            if not isinstance(content, str):
                raise FormatError("Unsupported session content")
            timestamp = str(message.get("timestamp", "unknown"))[:64].replace("\n", " ").replace("\r", " ")
            start = len(body_lines) + 1
            body_lines.append(f"## {timestamp} — {role}")
            body_lines.extend(content.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
            mapping.append({"source_line": line_no, "line_start": start, "line_end": len(body_lines)})
            body_lines.append("")
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FormatError("Malformed UTF-8 session JSONL") from exc
    if not mapping:
        raise FormatError("Session has no messages to import")
    return "\n".join(body_lines), mapping, session_key


def migrate_source(store: BrainStore, source_ref: str, *, stage: bool = False) -> MigrationResult:
    """Preview with zero writes; stage only snapshots and pending import records.

    No LLM call, index creation or fact promotion. Re-run repairs an interrupted
    stage using immutable deterministic outputs. A changed source gets a new job.
    """
    source_path = resolve_ref(store.workspace, source_ref)
    if source_path.name.casefold() in {"user.md", "soul.md"}:
        raise FormatError("Identity and personality files cannot be migrated")
    if source_path.is_relative_to(store.root):
        raise ScopeError("Cannot import the Brain Memory store into itself")
    if not source_path.is_file() or source_path.suffix.lower() not in {".jsonl", ".md"}:
        raise FormatError("Select a regular Markdown or session JSONL file")
    with source_path.open("rb") as stream:
        raw = stream.read(store.config.max_source_bytes + 1)
    if len(raw) > store.config.max_source_bytes:
        raise FormatError("Source exceeds byte budget")
    source_hash = content_hash(raw)
    job_id = "import-" + canonical_hash({
        "schema_version": 1, "source_ref": source_ref, "source_hash": source_hash,
    })[7:]
    is_session = source_path.suffix.lower() == ".jsonl"
    if is_session:
        body, mapping, session_key = _conversation(raw)
    else:
        try:
            body = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeError as exc:
            raise FormatError("Malformed UTF-8 Markdown") from exc
        mapping, session_key = [], ""
    metadata = {
        "schema_version": 1, "id": job_id, "status": "pending",
        "source_ref": source_ref, "source_hash": source_hash, "source_session": session_key,
    }
    document = render_markdown(metadata, body).encode("utf-8")
    if len(document) > store.config.max_document_bytes:
        raise FormatError("Normalized document exceeds byte budget")
    _, _, first_line = parse_markdown(document.decode("utf-8"))
    for span in mapping:
        span["line_start"] += first_line - 1
        span["line_end"] += first_line - 1
    snapshot_ref = f"conversations/{job_id}.md" if is_session else f"imports/{job_id}.md"
    record = {
        **metadata, "kind": "migration_candidate", "snapshot_ref": snapshot_ref,
        "snapshot_hash": content_hash(document), "line_mapping": mapping,
        "facts": [], "learning_performed": False,
    }
    record_bytes = (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    created = False
    if stage:
        with store.locked():
            # Write immutable snapshot and pending record first. If interrupted,
            # re-running verifies them and safely completes the ledger last.
            store._write_locked(snapshot_ref, document)
            created = store._write_locked(f"manifests/pending/{job_id}.json", record_bytes)
            ledger_ref = ".migration/manifest.json"
            ledger = {"schema_version": 1, "jobs": {}}
            old = None
            if store.path(ledger_ref).exists():
                old = store.read_bytes(ledger_ref)
                try:
                    ledger = json.loads(old)
                    if ledger["schema_version"] != 1 or not isinstance(ledger["jobs"], dict):
                        raise ValueError("unsupported ledger")
                except (ValueError, TypeError, KeyError) as exc:
                    raise FormatError("Migration ledger is invalid; keep outputs for recovery") from exc
            ledger["jobs"][job_id] = record
            updated = (json.dumps(ledger, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
            if old is not None and old != updated:
                store._write_locked(f".migration/backups/{content_hash(old)[7:]}.json", old)
            store._write_locked(ledger_ref, updated, replace=True)
    return MigrationResult(job_id, source_ref, source_hash, snapshot_ref, stage, created)
