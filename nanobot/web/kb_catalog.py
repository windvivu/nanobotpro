"""Atomic publication catalog for administrator-supplied customer knowledge."""

import asyncio
import hashlib
import io
import json
import os
import re
import secrets
import tempfile
import threading
import weakref
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from nanobot.brain_memory.config import BrainMemoryConfig
from nanobot.brain_memory.dashboard import learning_status, retire_facts, retire_facts_by_source
from nanobot.brain_memory.errors import BrainMemoryError, ConflictError, FormatError
from nanobot.brain_memory.migration import migrate_source, migration_batch_id, write_migration_batch
from nanobot.brain_memory.paths import resolve_ref
from nanobot.brain_memory.schemas import ConversationEvent
from nanobot.brain_memory.store import BrainStore
from nanobot.security.public_output import public_error

MAX_UPLOAD_BYTES = 4 * 1024 * 1024
_publish_locks = weakref.WeakValueDictionary()
_progress_cache = {}
_progress_cache_lock = threading.RLock()


def _progress_key(workspace, config, doc):
    return (str(Path(workspace).resolve()), config.root, doc["batch_id"], doc["file"])


def _invalidate_progress(workspace, doc):
    """Caller holds the workspace catalog lock, including during polling."""
    workspace_key = str(Path(workspace).resolve())
    with _progress_cache_lock:
        for key in list(_progress_cache):
            if key[0] == workspace_key and key[2] == doc.get("batch_id"):
                _progress_cache.pop(key, None)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _root(workspace):
    root = resolve_ref(Path(workspace).resolve(), "kb")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _read(root):
    path = root / "catalog.json"
    if path.is_symlink():
        raise FormatError("KB catalog must not be a symlink")
    if not path.exists():
        return {"documents": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("documents"), dict):
        raise FormatError("Invalid KB catalog")
    for key, doc in value["documents"].items():
        if (
            not re.fullmatch(r"[a-f0-9]{12}", key)
            or not isinstance(doc, dict)
            or doc.get("file") != f"kb/{key}.md"
            or doc.get("status") not in {"pending", "published", "revoked"}
            or not isinstance(doc.get("name"), str)
        ):
            raise FormatError("Invalid KB document")
    return value


def _save(root, catalog):
    fd, filename = tempfile.mkstemp(prefix=".catalog-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(catalog, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(filename, root / "catalog.json")
    finally:
        Path(filename).unlink(missing_ok=True)


@contextmanager
def _catalog(workspace):
    root = _root(workspace)
    lock_path = root / ".catalog.lock"
    if lock_path.is_symlink():
        raise FormatError("Invalid catalog lock")
    with FileLock(str(lock_path), timeout=5):
        yield root, _read(root)


def list_documents(workspace):
    # Reads never create a KB directory (chat attachment/retrieval paths use this).
    root = resolve_ref(Path(workspace).resolve(), "kb")
    return [dict(doc, document_id=key) for key, doc in _read(root)["documents"].items()]


def _extract(original_name, data, budget):
    suffix = Path(original_name).suffix.lower()
    if suffix in {".md", ".txt"}:
        return data.decode("utf-8-sig")
    if suffix == ".docx":
        try:
            from docx import Document
            from docx.table import Table
            from docx.text.paragraph import Paragraph
        except ImportError as exc:
            raise FormatError("Install python-docx to import Word documents") from exc
        from zipfile import ZipFile

        with ZipFile(io.BytesIO(data)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 32 * 1024 * 1024:
                raise FormatError("Word archive expands beyond import budget")
        doc = Document(io.BytesIO(data))
        parts = []
        for item in doc.iter_inner_content():
            if isinstance(item, Paragraph):
                parts.append(item.text)
            elif isinstance(item, Table):
                parts.extend(" | ".join(cell.text for cell in row.cells) for row in item.rows)
        return "\n".join(parts)
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise FormatError("Install pypdf to import PDF documents") from exc
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise FormatError("Encrypted PDF is not supported")
        parts, size = [], 0
        for page in reader.pages:
            text = page.extract_text() or ""
            size += len(text.encode("utf-8")) + 1
            if size > budget:
                raise FormatError("Extracted text exceeds max_document_bytes")
            parts.append(text)
        return "\n".join(parts)
    raise FormatError("Supported files: .md, .txt, .docx, .pdf")


def add_document(workspace, original_name, data: bytes, name=None, *, config=None):
    if (
        not isinstance(original_name, str)
        or not original_name.strip()
        or any(c in original_name for c in "/\\:\x00\r\n")
        or original_name in {".", ".."}
    ):
        raise FormatError("Filename must not contain a path")
    if len(data) > MAX_UPLOAD_BYTES:
        raise FormatError("File exceeds 4 MB")
    name = (name if name is not None else Path(original_name).stem).strip()
    if not name or len(name) > 200 or any(ord(c) < 32 for c in name):
        raise FormatError("Public name must contain 1–200 characters on one line")
    digest = hashlib.sha256(data).hexdigest()
    budget = (config or BrainMemoryConfig()).max_document_bytes
    try:
        text = _extract(original_name, data, budget)
    except FormatError:
        raise
    except Exception as exc:
        raise FormatError("Document cannot be parsed") from exc
    if not text.strip():
        raise FormatError("Document has no extractable text (scanned PDFs require OCR)")
    content = f"# {name}\n\n{text}".encode("utf-8")
    if len(content) > budget:
        raise FormatError("Extracted text exceeds max_document_bytes")
    with _catalog(workspace) as (root, catalog):
        for key, doc in catalog["documents"].items():
            if doc["sha256"] == digest:
                return dict(doc, document_id=key)
        document_id = secrets.token_hex(6)
        path = root / f"{document_id}.md"
        doc = dict(
            name=name,
            file=f"kb/{document_id}.md",
            original_name=original_name,
            sha256=digest,
            size=len(data),
            version=1,
            status="pending",
            batch_id="",
            published_at=None,
            revoked_at=None,
            fact_count=0,
        )
        with path.open("xb") as stream:
            stream.write(content)
        try:
            catalog["documents"][document_id] = doc
            _save(root, catalog)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return dict(doc, document_id=document_id)


def _document(catalog, document_id):
    if document_id not in catalog["documents"]:
        raise KeyError("KB document not found")
    return catalog["documents"][document_id]


def set_status(workspace, document_id, status, **fields):
    if status not in {"pending", "published", "revoked"}:
        raise ValueError("Invalid KB status")
    if fields.keys() - {"batch_id", "published_at", "revoked_at", "fact_count"}:
        raise ValueError("Invalid KB update")
    with _catalog(workspace) as (root, catalog):
        doc = _document(catalog, document_id)
        _invalidate_progress(workspace, doc)
        doc.update(status=status, **fields)
        _invalidate_progress(workspace, doc)
        _save(root, catalog)
        return dict(doc, document_id=document_id)


def published_paths(workspace: Path) -> set[Path]:
    try:
        return {
            resolve_ref(Path(workspace).resolve(), doc["file"])
            for doc in list_documents(workspace)
            if doc["status"] == "published"
            and resolve_ref(Path(workspace).resolve(), doc["file"]).is_file()
        }
    except (OSError, ValueError, TypeError, KeyError):
        return set()


def public_name(workspace: Path, source_ref: str) -> str | None:
    try:
        return next(
            (
                doc["name"]
                for doc in list_documents(workspace)
                if doc["file"] == source_ref and doc["status"] == "published"
            ),
            None,
        )
    except (OSError, ValueError, TypeError, KeyError):
        return None


def remove_document(workspace, document_id):
    with _catalog(workspace) as (root, catalog):
        doc = _document(catalog, document_id)
        if doc.get("cleanup_pending") or doc["status"] == "published" or (doc["status"] == "pending" and doc["batch_id"]):
            raise ConflictError("Revoke the published or queued document before deleting it")
        path = resolve_ref(Path(workspace).resolve(), doc["file"])
        del catalog["documents"][document_id]
        _save(root, catalog)
        path.unlink(missing_ok=True)


async def publish_document(workspace, config, runtime, document_id):
    for flag in ("enabled", "learning_enabled", "migration_enabled"):
        if not getattr(config, flag):
            raise ConflictError(f"brainMemory.{flag} must be enabled")
    if runtime is None:
        raise ConflictError("Learning runtime is unavailable")
    key = str(Path(workspace).resolve())
    async with _publish_locks.setdefault(key, asyncio.Lock()):
        docs = {doc["document_id"]: doc for doc in list_documents(workspace)}
        doc = _document({"documents": docs}, document_id)
        if doc["status"] == "published":
            with _catalog(workspace):
                _invalidate_progress(workspace, doc)
            return doc
        if doc["status"] == "revoked":
            raise ConflictError("Upload a new document version after revocation")
        store = BrainStore(Path(workspace), config)
        result = migrate_source(store, doc["file"], stage=True)
        batch_id = migration_batch_id([result])
        write_migration_batch(store, [result], batch_id)
        # Persist before yielding: delete cannot remove a source already queued.
        set_status(workspace, document_id, "pending", batch_id=batch_id)
        jobs = await runtime.enqueue_migration_jobs(result, batch_id=batch_id, published_kb=True)
        write_migration_batch(store, [result], batch_id, jobs)
        return set_status(
            workspace, document_id, "published", published_at=_now(), batch_id=batch_id
        )


def revoke_document(workspace, config, document_id):
    operation = _publish_locks.get(str(Path(workspace).resolve()))
    if operation is not None and operation.locked():
        raise ConflictError("Knowledge is being queued; wait before revoking")
    with _catalog(workspace) as (root, catalog):
        doc = _document(catalog, document_id)
        if doc["status"] == "published":
            if doc["batch_id"]:
                progress = learning_status(Path(workspace), config, batch_id=doc["batch_id"])
                if progress["latest_migration"]["in_progress"]:
                    raise ConflictError("Đợi học xong rồi thu hồi (wait for learning to finish)")
            # Persist the customer boundary before any best-effort file cleanup.
            doc.update(status="revoked", revoked_at=_now(), cleanup_pending=True)
            _invalidate_progress(workspace, doc)
            _save(root, catalog)
        elif doc["status"] != "revoked":
            raise ConflictError("Only published documents can be revoked")
        elif not doc.get("cleanup_pending"):
            return dict(doc, document_id=document_id)
        try:
            result = retire_facts_by_source(Path(workspace), config, doc["file"])
            retired_ids = set(doc.get("retired_fact_ids", [])) | set(result["retired_fact_ids"])
            # A previous cleanup may have moved some files before an I/O error.
            # Reconcile its audit trail so a successful retry reports all retired facts.
            store = BrainStore(Path(workspace), config)
            for path in store.path(".migration/trash").glob(f"source-{document_id}-*/retired.json"):
                audit = json.loads(store.read_bytes(path.relative_to(store.root).as_posix()))
                if audit.get("source_ref") == doc["file"]:
                    retired_ids.update(audit.get("retired_fact_ids", []))
        except (BrainMemoryError, OSError, ValueError) as exc:
            public_error(exc)
            # Retry via revoke, never republish a document whose cleanup failed.
            return dict(doc, document_id=document_id)
        doc.update(fact_count=0, cleanup_pending=False,
                   retired_fact_ids=sorted(retired_ids))
        _save(root, catalog)
        return dict(doc, document_id=document_id)


# A note the admin types on the page is a small document of its own: it is learned, listed,
# revoked and deleted like any other, and the facts it replaces are retired by ID (custom).
NOTE_PREFIX = "note-"
NOTE_MAX_CHARS = 2000


async def add_note(workspace, config, runtime, text):
    """Publish one admin note and return it with the facts already in the KB on the same subject."""
    if not isinstance(text, str) or not text.strip() or len(text) > NOTE_MAX_CHARS:
        raise FormatError(f"Note must contain 1–{NOTE_MAX_CHARS} characters")
    text = text.strip()
    first = " ".join(text.split())
    name = first if len(first) <= 120 else first[:117].rstrip() + "..."
    doc = add_document(workspace, f"{NOTE_PREFIX}{secrets.token_hex(4)}.md", text.encode("utf-8"), name=name, config=config)
    if doc["status"] == "pending" and not doc["batch_id"]:
        doc = await publish_document(workspace, config, runtime, doc["document_id"])
    elif doc["status"] == "revoked":
        raise ConflictError("This note was revoked before; delete it in the table, then teach it again")
    related = related_facts(workspace, config, text, exclude_file=doc["file"])
    return {"document": doc, "related": related}


def related_facts(workspace, config, text, *, exclude_file=None, limit=8):
    """Published facts the retrieval would show next to this text: what a note may replace."""
    from nanobot.brain_memory.index import BrainIndex
    store = BrainStore(Path(workspace), config.model_copy(update={"enabled": True, "retrieval_enabled": True}))
    allowed = published_paths(workspace)
    found, seen = [], set()
    for item in BrainIndex(store).search(text[:config.max_query_chars], limit=limit):
        if item.fact_id in seen:
            continue
        seen.add(item.fact_id)
        try:
            fact = store.read_fact(item.fact_id)
        except (BrainMemoryError, ValueError):
            continue
        ref = fact.source.source_ref
        if fact.status != "active" or ref == exclude_file or (store.workspace / ref).resolve() not in allowed:
            continue
        found.append({"id": fact.id, "title": fact.title, "body": fact.body,
                      "document": public_name(Path(workspace), ref) or ""})
    return found


def retire_kb_facts(workspace, config, fact_ids):
    """Retire chosen facts and forget the cached counts of the documents they came from."""
    result = retire_facts(Path(workspace), config, list(fact_ids))
    with _catalog(workspace) as (_root_dir, catalog):
        for doc in catalog["documents"].values():
            if doc["file"] in result["source_refs"]:
                _invalidate_progress(workspace, doc)
    return {"retired_fact_ids": result["retired_fact_ids"]}


def _batch_jobs(store, doc):
    if not doc.get("batch_id"):
        return []
    batch = json.loads(store.read_bytes(f".migration/batches/{doc['batch_id']}.json"))
    jobs = []
    for job_id in dict.fromkeys(batch.get("learning_job_ids", [])):
        if not isinstance(job_id, str) or not re.fullmatch(r"learn-[a-f0-9]{64}", job_id):
            raise FormatError("Invalid KB job ID")
        job = json.loads(store.read_bytes(f"manifests/pending/jobs/{job_id}.json"))
        event = ConversationEvent.model_validate(job["event"])
        if (job.get("job_id") != job_id or event.migration_batch_id != doc["batch_id"]
                or event.source.source_ref != doc["file"]):
            raise FormatError("KB job provenance mismatch")
        jobs.append(job)
    return jobs


def _learning(jobs):
    done = sum(job.get("status") in {"accepted", "rejected", "quarantined", "skipped"} for job in jobs)
    failed = sum(job.get("status") == "failed" for job in jobs)
    return {"total": len(jobs), "done": done, "failed": failed,
            "in_progress": len(jobs) - done - failed}


def _learning_detail(jobs, max_attempts):
    """Where the unfinished or failed part of a document stands, for the admin."""
    focus = [job for job in jobs if job.get("status") in {"pending", "retry"}]
    focus = focus or [job for job in jobs if job.get("status") == "failed"]
    if not focus:
        return None
    job = max(focus, key=lambda item: str(item.get("updated_at", "")))
    attempts = job.get("attempts", 0)
    return {
        "phase": str(job.get("phase", ""))[:40],
        "attempts": attempts if type(attempts) is int else 0,
        "max_attempts": max_attempts,
        "last_error": str(job.get("error_type", ""))[:80],
    }


def document_facts(workspace, config, document_id):
    """What the bot learned from one document, in learning order: the admin checks the facts
    behind a count, and sees what a customer can be told."""
    store = BrainStore(Path(workspace), config)
    with _catalog(workspace) as (_root_dir, catalog):
        doc = dict(catalog["documents"][document_id])
    facts = []
    for job in _batch_jobs(store, doc):
        for fact_id in job.get("created_fact_ids", []):
            try:
                fact = store.read_fact(fact_id)
            except (BrainMemoryError, ValueError):
                continue  # retired with its document, or never written
            if fact.status == "active" and fact.source.source_ref == doc["file"]:
                facts.append({"id": fact.id, "title": fact.title, "body": fact.body, "topic": fact.topic})
    return facts


def refresh_fact_counts(workspace, config, worker=None, *, tolerate_errors=False):
    """Read each document's batch, including partial completion and failure.

    Learning is a derived response field, never persisted into the catalog.
    Dashboard callers opt into per-document errors; strict callers retain
    provenance validation exceptions. Only completed, successful reads cache.
    """
    store = BrainStore(Path(workspace), config)
    documents = []
    with _catalog(workspace) as (root, catalog):
        changed = False
        for key, doc in catalog["documents"].items():
            progress, count, detail = _learning([]), doc["fact_count"], None
            if doc["status"] == "published":
                cache_key = _progress_key(workspace, config, doc)
                with _progress_cache_lock:
                    cached = _progress_cache.get(cache_key)
                try:
                    if cached is None:
                        jobs = _batch_jobs(store, doc)
                        progress = _learning(jobs)
                        detail = _learning_detail(jobs, config.learning_max_attempts)
                        if jobs:
                            # Facts retired one by one (replaced by a note) no longer count
                            count = len({fact_id for job in jobs for fact_id in job.get("created_fact_ids", [])
                                         if store.path(f"facts/{fact_id}.md").is_file()})
                        if jobs and not progress["in_progress"] and not progress["failed"]:
                            with _progress_cache_lock:
                                _progress_cache[cache_key] = (dict(progress), count)
                    else:
                        progress, count = dict(cached[0]), cached[1]
                except (BrainMemoryError, OSError, ValueError, TypeError, KeyError, AttributeError):
                    if not tolerate_errors:
                        raise
                    progress = dict(_learning([]), error="unreadable")
            if count != doc["fact_count"]:
                doc["fact_count"] = count
                changed = True
            documents.append(dict(doc, document_id=key, learning=progress, learning_detail=detail))
        if changed:
            _save(root, catalog)
    return documents


async def relearn_document(workspace, config, runtime, document_id):
    """Explicit retry of only failed jobs in the document's existing batch."""
    if runtime is None:
        raise ConflictError("Learning runtime is unavailable")
    key = str(Path(workspace).resolve())
    async with _publish_locks.setdefault(key, asyncio.Lock()):
        docs = {doc["document_id"]: doc for doc in list_documents(workspace)}
        doc = _document({"documents": docs}, document_id)
        if doc["status"] != "published":
            raise ConflictError("Only published documents can be relearned")
        jobs = _batch_jobs(BrainStore(Path(workspace), config), doc)
        failed = [job for job in jobs if job["status"] == "failed"]
        if not failed:
            raise ConflictError("Document has no failed learning jobs")
        queued = []
        with _catalog(workspace):
            _invalidate_progress(workspace, doc)
        try:
            for job in failed:
                queued.append(await runtime.enqueue(ConversationEvent.model_validate(job["event"])))
        finally:
            with _catalog(workspace):
                _invalidate_progress(workspace, doc)
        return {"document_id": document_id, "job_ids": queued}
