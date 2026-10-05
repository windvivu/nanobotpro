"""Dashboard operations: explicit source selection and isolated settings writes."""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock
from pydantic import TypeAdapter, ValidationError

from nanobot.brain_memory.config import BrainMemoryConfig
from nanobot.brain_memory.errors import ConflictError, FormatError
from nanobot.brain_memory.index import BrainIndex
from nanobot.brain_memory.migration import current_promotion_sequence
from nanobot.brain_memory.paths import resolve_ref
from nanobot.brain_memory.schemas import FactId, Manifest, canonical_hash, content_hash
from nanobot.brain_memory.store import BrainStore

_AUTO_SYNC_MEMORY = frozenset({"memory.md", "memory/memory.md"})
_TERMINAL_JOB_STATUSES = frozenset({"accepted", "rejected", "quarantined", "failed", "skipped"})


def _safe_json(store: BrainStore, path: Path) -> dict:
    ref = path.relative_to(store.root).as_posix()
    if path.is_symlink() or store.path(ref) != path.resolve():
        return {}
    value = json.loads(store.read_bytes(ref).decode("utf-8"))
    return value if isinstance(value, dict) else {}


def _job_summary(store: BrainStore, path: Path) -> dict | None:
    try:
        record = _safe_json(store, path)
        if not record:
            return None
        event = record.get("event", {})
        source = event.get("source", {})
        if not isinstance(record, dict) or not isinstance(event, dict) or not isinstance(source, dict):
            return None
        history = record.get("phase_history", [])
        if not isinstance(history, list):
            history = []
        return {
            "job_id": record.get("job_id", path.stem),
            "source_ref": source.get("source_ref", ""),
            "migration_batch_id": event.get("migration_batch_id", ""),
            "status": record.get("status", "unknown"),
            "phase": record.get("phase", "unknown"),
            "attempts": record.get("attempts", 0),
            "created_at": record.get("created_at", ""),
            "updated_at": record.get("updated_at", record.get("created_at", "")),
            "error_type": record.get("error_type", ""),
            "started_at": record.get("started_at", ""),
            "last_finished_at": record.get("last_finished_at", ""),
            "duration_ms": record.get("last_duration_ms", 0),
            "total_duration_ms": record.get("total_duration_ms", 0),
            "phase_history": history[-32:],
        }
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return None


def learning_status(workspace: Path, config: BrainMemoryConfig, worker=None) -> dict:
    """Return safe worker metadata and guarded undo state for the dashboard."""
    store = BrainStore(workspace.resolve(), config)
    jobs_dir = store.root / "manifests" / "pending" / "jobs"
    jobs = []
    if jobs_dir.is_dir():
        for index, path in enumerate(jobs_dir.glob("learn-*.json")):
            if index >= 512:
                break
            summary = _job_summary(store, path)
            if summary:
                jobs.append(summary)
    jobs.sort(key=lambda item: item["updated_at"], reverse=True)

    batches_dir = store.root / ".migration" / "batches"
    latest = None
    if batches_dir.is_dir():
        records = []
        for path in batches_dir.glob("batch-*.json"):
            try:
                record = _safe_json(store, path)
                if isinstance(record, dict) and record.get("schema_version") == 1:
                    records.append(record)
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                continue
        if records:
            latest = max(records, key=lambda item: str(item.get("created_at", "")))

    migration = None
    if latest:
        job_records = []
        for job_id in latest.get("learning_job_ids", []):
            if not isinstance(job_id, str):
                continue
            job_path = jobs_dir / f"{job_id}.json"
            summary = _job_summary(store, job_path)
            if summary:
                try:
                    raw = _safe_json(store, job_path)
                except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                    raw = {}
                job_records.append((summary, raw))
        accepted = [raw for _, raw in job_records if raw.get("status") == "accepted"]
        created_ids = sorted({
            fact_id for raw in accepted
            for fact_id in raw.get("created_fact_ids", [])
            if isinstance(fact_id, str)
        })
        sequences = [
            raw.get("promotion_sequence") for raw in accepted
            if type(raw.get("promotion_sequence")) is int
        ]
        all_terminal = bool(job_records) and all(
            summary["status"] in _TERMINAL_JOB_STATUSES for summary, _ in job_records
        )
        in_progress = bool(job_records) and not all_terminal
        current_sequence = current_promotion_sequence(store)
        max_sequence = max(sequences) if sequences else None
        reason = "ready"
        undo_available = False
        if latest.get("undone"):
            reason = "already_undone"
        elif not all_terminal:
            reason = "learning_in_progress"
        elif max_sequence is None:
            reason = "no_accepted_promotion"
        elif current_sequence != max_sequence:
            reason = "protected_by_later_promotion"
        elif not created_ids:
            reason = "no_new_facts"
        else:
            undo_available = True
        migration = {
            "batch_id": latest.get("batch_id", ""),
            "source_refs": latest.get("source_refs", []),
            "created_at": latest.get("created_at", ""),
            "undone": bool(latest.get("undone")),
            "job_count": len(job_records),
            "accepted_count": len(accepted),
            "created_fact_count": len(created_ids),
            "in_progress": in_progress,
            "undo_available": undo_available,
            "undo_reason": reason,
        }
    worker_state = worker.stats() if worker is not None and hasattr(worker, "stats") else {
        "running": False,
        "stopping": False,
        "queue_depth": 0,
        "queue_capacity": config.queue_capacity,
        "processing": 0,
    }
    return {"jobs": jobs[:100], "latest_migration": migration, "worker": worker_state}


def undo_migration_batch(workspace: Path, config: BrainMemoryConfig, batch_id: str) -> dict:
    """Move only newly-created facts from a guarded latest migration to trash."""
    effective = config.model_copy(update={"enabled": True})
    store = BrainStore(workspace.resolve(), effective)
    batch_ref = f".migration/batches/{batch_id}.json"
    with store.locked():
        try:
            batch = json.loads(store.read_bytes(batch_ref).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, OSError, ValueError) as exc:
            raise FormatError("Migration batch is invalid") from exc
        if batch.get("schema_version") != 1 or batch.get("batch_id") != batch_id:
            raise FormatError("Migration batch is invalid")
        if batch.get("undone"):
            raise ConflictError("Migration batch was already undone")
        batch_dir = store.root / ".migration" / "batches"
        latest_batch_id = batch_id
        latest_created_at = str(batch.get("created_at", ""))
        if batch_dir.is_dir():
            for candidate in batch_dir.glob("batch-*.json"):
                try:
                    candidate_record = _safe_json(store, candidate)
                    candidate_created_at = str(candidate_record.get("created_at", ""))
                    if candidate_created_at > latest_created_at:
                        latest_created_at = candidate_created_at
                        latest_batch_id = candidate_record.get("batch_id", candidate.stem)
                except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
                    continue
        if latest_batch_id != batch_id:
            raise ConflictError("Only the latest migration batch can be undone")
        jobs_dir = store.root / "manifests" / "pending" / "jobs"
        job_records = []
        for job_id in batch.get("learning_job_ids", []):
            if not isinstance(job_id, str):
                raise FormatError("Migration batch job is invalid")
            try:
                raw = _safe_json(store, jobs_dir / f"{job_id}.json")
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise ConflictError("Migration learning job is unavailable") from exc
            job_records.append(raw)
        if not job_records or any(raw.get("status") not in _TERMINAL_JOB_STATUSES for raw in job_records):
            raise ConflictError("Migration learning is still in progress")
        accepted = [raw for raw in job_records if raw.get("status") == "accepted"]
        sequences = [raw.get("promotion_sequence") for raw in accepted if type(raw.get("promotion_sequence")) is int]
        if not sequences or current_promotion_sequence(store) != max(sequences):
            raise ConflictError("A later promotion protected the graph")
        fact_ids = sorted({
            fact_id for raw in accepted
            for fact_id in raw.get("created_fact_ids", [])
            if isinstance(fact_id, str)
        })
        if not fact_ids:
            raise ConflictError("Migration created no new facts")
        trash_dir = store.path(f".migration/trash/{batch_id}")
        trash_dir.mkdir(parents=True, exist_ok=True)
        moved = []
        for fact_id in fact_ids:
            try:
                validated_id = TypeAdapter(FactId).validate_python(fact_id)
            except ValidationError as exc:
                raise FormatError("Migration fact ID is invalid") from exc
            source = store.path(f"facts/{validated_id}.md")
            if source.is_symlink() or not source.is_file():
                continue
            target = trash_dir / f"{validated_id}.md"
            os.replace(source, target)
            moved.append(validated_id)
        if not moved:
            raise ConflictError("Migration facts are no longer present")
        batch["undone"] = True
        batch["undone_at"] = datetime.now(timezone.utc).isoformat()
        batch["undo_fact_ids"] = moved
        store._write_locked(
            batch_ref,
            (json.dumps(batch, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
            replace=True,
        )
    try:
        BrainIndex(store).rebuild()
    except (OSError, ValueError, FormatError):
        # The Markdown graph remains correct; retrieval can rebuild on demand.
        pass
    return {"batch_id": batch_id, "undone_fact_ids": moved}


def _processed_hashes(
    store: BrainStore,
) -> tuple[set[tuple[str, str]], set[tuple[str, str]], set[str], set[str], set[str]]:
    """Return staged/promoted hashes and source refs seen by migration.

    Hashes describe one immutable source version.  The source-ref sets retain
    the history needed for manual migration policy: a session may change its
    hash after another chat turn, but it is complete once that source was both
    staged and promoted once.
    """
    staged: set[tuple[str, str]] = set()
    promoted: set[tuple[str, str]] = set()
    staged_refs: set[str] = set()
    promoted_refs: set[str] = set()
    empty_manifest_refs: set[str] = set()
    try:
        ledger = json.loads(store.read_bytes(".migration/manifest.json").decode("utf-8"))
        for record in ledger.get("jobs", {}).values():
            if isinstance(record, dict) and isinstance(record.get("source_ref"), str) and isinstance(record.get("source_hash"), str):
                staged.add((record["source_ref"], record["source_hash"]))
                staged_refs.add(record["source_ref"])
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, ValueError):
        pass
    accepted_dir = store.root / "manifests" / "accepted"
    if accepted_dir.is_dir():
        for path in accepted_dir.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("schema_version") != 1 or record.get("status") != "accepted":
                    continue
                manifest = Manifest.model_validate(record.get("manifest"))
                manifest_payload = manifest.model_dump(mode="json")
                if not manifest.content_ref:
                    manifest_payload.pop("content_ref", None)
                if manifest.content_hash is None:
                    manifest_payload.pop("content_hash", None)
                digest = canonical_hash(manifest_payload)
                if path.stem != digest.removeprefix("sha256:"):
                    continue
                fact_ids = record.get("fact_ids")
                if not isinstance(fact_ids, list) or not fact_ids:
                    continue
                for fact_id in fact_ids:
                    validated_id = TypeAdapter(FactId).validate_python(fact_id)
                    store.read_fact(validated_id)
                source_ref = manifest.source.source_ref
                source_hash = manifest.source.source_hash
                promoted.add((source_ref, source_hash))
                promoted_refs.add(source_ref)
            except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError, ValidationError):
                continue
    rejected_dir = store.root / "manifests" / "rejected"
    if rejected_dir.is_dir():
        for path in rejected_dir.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                reasons = record.get("reasons", [])
                manifest = record.get("manifest")
                source = manifest.get("source") if isinstance(manifest, dict) else None
                source_ref = source.get("source_ref") if isinstance(source, dict) else None
                if (
                    record.get("schema_version") == 1
                    and record.get("status") == "rejected"
                    and isinstance(reasons, list)
                    and "empty_manifest" in reasons
                    and isinstance(source_ref, str)
                ):
                    empty_manifest_refs.add(source_ref)
            except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
                continue
    facts_dir = store.root / "facts"
    if facts_dir.is_dir():
        for path in facts_dir.glob("*.md"):
            try:
                fact = store.read_fact(path.stem)
                if fact.status in {"active", "superseded"}:
                    promoted.add((fact.source.source_ref, fact.source.source_hash))
                    promoted_refs.add(fact.source.source_ref)
            except (OSError, UnicodeError, ValueError):
                continue
    return staged, promoted, staged_refs, promoted_refs, empty_manifest_refs


def sources(workspace: Path, config: BrainMemoryConfig) -> list[dict]:
    """List legacy memory and session metadata only; never return chat bodies."""
    workspace = workspace.resolve()
    brain_root = resolve_ref(workspace, config.root)
    store = BrainStore(workspace, config)
    staged, promoted, staged_refs, promoted_refs, empty_manifest_refs = _processed_hashes(store)
    # Identity/personality files are instructions, not durable facts. Never
    # expose them as migration candidates for Brain Memory.
    candidates = ["MEMORY.md", "memory/MEMORY.md", "memory/HISTORY.md", *config.migration_sources]
    candidates = list(dict.fromkeys(candidates))
    sessions = resolve_ref(workspace, "sessions")
    if sessions.is_dir():
        candidates.extend(
            f"sessions/{p.name}" for p in sorted(sessions.glob("*.jsonl"))[:500]
            if f"sessions/{p.name}" not in candidates
        )
    result = []
    for ref in candidates:
        try:
            path = resolve_ref(workspace, ref)
            if not path.is_file() or path.is_relative_to(brain_root):
                continue
            size = path.stat().st_size
            source_hash = content_hash(path.read_bytes()) if size <= config.max_source_bytes else ""
            session_id = ""
            if ref.endswith(".jsonl"):
                with path.open("r", encoding="utf-8-sig") as stream:
                    header = json.loads(stream.readline(65536))
                if header.get("_type") != "metadata" or not isinstance(header.get("key"), str):
                    continue
                session_id = header["key"]
            key = (ref, source_hash)
            result.append({
                "source_ref": ref,
                "session_id": session_id,
                "bytes": size,
                "source_hash": source_hash,
                "importable": size <= config.max_source_bytes,
                "staged": bool(source_hash and key in staged),
                "staged_once": ref in staged_refs,
                "promoted": bool(source_hash and key in promoted),
                "promoted_once": ref in promoted_refs,
                "migration_completed": ref in staged_refs and (
                    ref in promoted_refs or ref in empty_manifest_refs
                ),
                "auto_sync": ref.casefold() in _AUTO_SYNC_MEMORY,
            })
        except (OSError, ValueError, AttributeError):
            continue
    return result


def save_settings(path: Path, fallback, changes: dict) -> BrainMemoryConfig:
    """Merge only Brain Memory into the latest config; never rewrite other sections."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".brain.lock", timeout=5):
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else fallback.model_dump(
            mode="json", by_alias=True
        )
        current = BrainMemoryConfig.model_validate(data.get("brainMemory", data.get("brain_memory", {})))
        updated = BrainMemoryConfig.model_validate({**current.model_dump(), **changes})
        data.pop("brain_memory", None)
        data["brainMemory"] = updated.model_dump(mode="json", by_alias=True)
        fd, name = tempfile.mkstemp(prefix=".brain-config-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
    return updated
