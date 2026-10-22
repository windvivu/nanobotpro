"""The internal bot's knowledge library on the Brain page (custom).

Documents and notes the admin teaches a bot that is not in customer service mode: the same catalog
code as the customer service KB (nanobot/web/kb_catalog.py), in its own folder, library/. Public
customer mode never reads or learns that folder, so an internal document cannot reach a customer,
even if the instance turns customer service on later. In that mode these routes answer 404 and the
Brain page points to the customer service page instead.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from nanobot.brain_memory.config import LIBRARY_FOLDER
from nanobot.brain_memory.errors import BrainMemoryError, ConflictError, DisabledError
from nanobot.web import kb_catalog
from nanobot.web.routes.memory_graph import _brain_config, _public_mode, _require_csrf

FOLDER = LIBRARY_FOLDER
# What learning a document needs from Brain, and what the bot needs to use it in a reply
NEEDED_SWITCHES = ("learning_enabled", "migration_enabled", "retrieval_enabled")


def _guard(request: Request) -> None:
    if _public_mode(request):
        raise HTTPException(404, "Customer service mode: the knowledge is on the customer service page")
    if request.method == "POST":
        _require_csrf(request)


router = APIRouter(prefix="/api/memory/library", dependencies=[Depends(_guard)])


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, "Document not found")
    if isinstance(exc, (ConflictError, DisabledError)):
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc) if isinstance(exc, BrainMemoryError) else "Invalid document")


def _workspace(request: Request):
    return request.app.state.config.workspace_path


def _runtime(request: Request):
    return getattr(getattr(request.app.state, "agent", None), "brain_runtime", None)


def _resync_tool(request: Request) -> None:
    """search_knowledge exists once something is learned here, and goes when the last is forgotten."""
    agent = getattr(request.app.state, "agent", None)
    if sync := getattr(agent, "_sync_knowledge_tool", None):
        sync()


def _readiness(config) -> dict:
    """Which Brain switches are off; the page offers to turn them on in one click."""
    missing = [name for name in ("enabled", *NEEDED_SWITCHES) if not getattr(config, name)]
    if config.retrieval_sessions is not None:
        missing.append("retrieval_sessions")  # read for some sessions only: not every chat uses it
    return {"ready": not missing, "missing": missing}


@router.get("")
async def library(request: Request):
    config = _brain_config(request)
    try:
        documents = await run_in_threadpool(
            kb_catalog.refresh_fact_counts, _workspace(request), config,
            getattr(_runtime(request), "worker", None), tolerate_errors=True, folder=FOLDER,
        )
    except (BrainMemoryError, ValueError, OSError) as exc:
        raise _error(exc) from exc
    for doc in documents:
        doc["kind"] = kb_catalog.document_kind(doc)
    return {"documents": documents, **_readiness(config)}


async def _learn(request: Request, doc: dict) -> dict:
    """An internal document is learned as soon as it is added (no publication step as for customers).
    When Brain cannot learn yet, it stays pending with the reason; Learn retries it. A pending one
    with a batch is one whose earlier learning stopped before its jobs were queued: queued again."""
    if doc["status"] == "revoked":
        return dict(doc, learn_error="Still being forgotten; press Forget again, then upload it again")
    if doc["status"] != "pending":
        return doc
    try:
        doc = await kb_catalog.publish_document(
            _workspace(request), _brain_config(request), _runtime(request), doc["document_id"], folder=FOLDER,
        )
    except (ConflictError, DisabledError) as exc:
        return dict(doc, learn_error=str(exc))
    _resync_tool(request)
    return doc


@router.post("/upload")
async def upload(request: Request, file: UploadFile = File(...), name: str | None = Form(None)):
    try:
        data = await file.read(kb_catalog.MAX_UPLOAD_BYTES + 1)
        add = lambda: kb_catalog.add_document(  # noqa: E731
            _workspace(request), file.filename, data, name, config=_brain_config(request), folder=FOLDER)
        doc = await run_in_threadpool(add)
        if doc["status"] == "revoked" and not doc.get("cleanup_pending"):
            # The same file again after Forget: learned anew, not silently kept forgotten (review)
            await run_in_threadpool(kb_catalog.remove_document, _workspace(request), doc["document_id"], folder=FOLDER)
            doc = await run_in_threadpool(add)
        return await _learn(request, doc)
    except (BrainMemoryError, ValueError, OSError, ImportError) as exc:
        raise _error(exc) from exc
    finally:
        await file.close()


class Note(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=kb_catalog.NOTE_MAX_CHARS)


class FactIds(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_ids: list[str] = Field(min_length=1, max_length=50)


@router.post("/teach")
async def teach(request: Request, note: Note):
    """A few lines, or a link to a page, become knowledge at once; the reply lists the facts of the
    library the note may replace."""
    try:
        result = await kb_catalog.add_note(
            _workspace(request), _brain_config(request), _runtime(request), note.text,
            origin="page", folder=FOLDER, renew_revoked=True,
        )
    except (BrainMemoryError, KeyError, ValueError, OSError) as exc:
        raise _error(exc) from exc
    _resync_tool(request)
    return result


@router.post("/facts/retire")
async def retire(request: Request, body: FactIds):
    try:
        return await run_in_threadpool(
            kb_catalog.retire_kb_facts, _workspace(request), _brain_config(request), body.fact_ids, folder=FOLDER,
        )
    except (BrainMemoryError, KeyError, ValueError, OSError) as exc:
        raise _error(exc) from exc


@router.get("/{document_id}/facts")
async def facts(request: Request, document_id: str):
    try:
        return {"facts": await run_in_threadpool(
            kb_catalog.document_facts, _workspace(request), _brain_config(request), document_id, folder=FOLDER,
        )}
    except (BrainMemoryError, KeyError, ValueError, OSError) as exc:
        raise _error(exc) from exc


@router.post("/{document_id}/{action}")
async def action(request: Request, document_id: str, action: str):
    workspace, config = _workspace(request), _brain_config(request)
    try:
        if action == "learn":
            result = await kb_catalog.publish_document(workspace, config, _runtime(request), document_id, folder=FOLDER)
        elif action == "forget":  # moves fact files and rebuilds the index: off the event loop (review)
            result = await run_in_threadpool(kb_catalog.revoke_document, workspace, config, document_id, folder=FOLDER)
        elif action == "relearn":
            return await kb_catalog.relearn_document(workspace, config, _runtime(request), document_id, folder=FOLDER)
        elif action == "delete":
            await run_in_threadpool(kb_catalog.remove_document, workspace, document_id, folder=FOLDER)
            result = {"deleted": True}
        else:
            raise HTTPException(404, "Unknown action")
    except (BrainMemoryError, KeyError, ValueError, OSError) as exc:
        raise _error(exc) from exc
    _resync_tool(request)
    return result
