"""Brain Memory graph API and the optional Canvas client."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from nanobot.brain_memory.dashboard import learning_status, sources, undo_migration_batch
from nanobot.brain_memory.errors import BrainMemoryError, ConflictError, DisabledError, FormatError
from nanobot.brain_memory.graph import BrainGraph, GraphFilters
from nanobot.brain_memory.migration import migrate_source, migration_batch_id, write_migration_batch
from nanobot.brain_memory.schemas import FactId
from nanobot.brain_memory.store import BrainStore
from nanobot.web.auth import csrf_valid

router = APIRouter()


class BrainSettingsPayload(BaseModel):
    enabled: bool | None = None
    retrieval_enabled: bool | None = None
    learning_enabled: bool | None = None
    learning_daily_job_limit: int | None = Field(default=None, ge=1, le=50)
    learning_input_bytes: int | None = Field(default=None, ge=16_384, le=131_072)
    retrieval_sessions: list[str] | None = Field(default=None, max_length=500)
    learning_sessions: list[str] | None = Field(default=None, max_length=500)
    migration_enabled: bool | None = None
    migration_sources: list[str] | None = Field(default=None, max_length=500)
    migration_stage_only: bool | None = None


def _require_csrf(request: Request) -> None:
    """Require a double-submit token for state-changing dashboard requests."""
    # With no dashboard password there is no browser session to bind a token
    # to, so local development remains usable without an artificial cookie.
    if getattr(request.app.state, "web_password", "") and not csrf_valid(request):
        raise HTTPException(status_code=403, detail="CSRF token required")


@router.get("/memory/graph", response_class=HTMLResponse)
async def memory_graph_page(request: Request):
    """Render the optional graph client; data still comes from the JSON API."""
    config = request.app.state.config.brain_memory
    return request.app.state.templates.TemplateResponse(
        request, "memory_graph.html", {
            "brain_memory": config,
            "graph_available": True,
        }
    )


def _snapshot(request: Request, *, topic: str | None, status: str,
              min_confidence: float, query: str | None = None,
              limit: int | None = None, refresh: bool = False):
    # Local read-only view, independent of bot processing flags. Building a
    # graph never creates the store/index or enqueues learning.
    config = request.app.state.config.brain_memory.model_copy(
        update={"enabled": True, "graph_enabled": True}
    )
    if status not in {"active", "superseded", "quarantined", "rejected"}:
        raise HTTPException(status_code=400, detail="Invalid graph status")
    try:
        store = BrainStore(request.app.state.config.workspace_path, config)
        return BrainGraph(store, config).build(
            GraphFilters(topic, status, min_confidence, limit, query), refresh=refresh
        )
    except DisabledError as exc:
        raise HTTPException(status_code=404, detail="Brain Memory graph is disabled") from exc
    except (BrainMemoryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid graph request") from exc


def _response(snapshot, max_bytes: int) -> JSONResponse:
    payload = snapshot.model_dump(mode="json")
    response = JSONResponse(payload)
    if len(response.body) > max_bytes:
        raise HTTPException(status_code=413, detail="Graph response exceeds configured budget")
    return response


@router.get("/api/memory/graph")
async def memory_graph(
    request: Request,
    topic: str | None = Query(default=None, max_length=200),
    status: str = Query(default="active", max_length=20),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    query: str | None = Query(default=None, max_length=200),
    limit: int | None = Query(default=None, ge=1, le=5000),
):
    snapshot = _snapshot(request, topic=topic, status=status, min_confidence=min_confidence, query=query, limit=limit)
    return _response(snapshot, request.app.state.config.brain_memory.max_response_bytes)


@router.get("/api/memory/settings")
async def memory_settings(request: Request):
    config = request.app.state.config.brain_memory
    workspace = request.app.state.config.workspace_path
    return {
        "enabled": config.enabled,
        "retrieval_enabled": config.retrieval_enabled,
        "learning_enabled": config.learning_enabled,
        "learning_daily_job_limit": config.learning_daily_job_limit,
        "learning_input_bytes": config.learning_input_bytes,
        "learning_max_attempts": config.learning_max_attempts,
        "retrieval_sessions": config.retrieval_sessions,
        "learning_sessions": config.learning_sessions,
        "migration_enabled": config.migration_enabled,
        "migration_sources": config.migration_sources,
        "migration_stage_only": config.migration_stage_only,
        "migration_delete_sources": False,
        "sources": sources(workspace, config),
    }


@router.post("/api/memory/settings")
async def update_memory_settings(request: Request, payload: BrainSettingsPayload):
    _require_csrf(request)
    from nanobot.config.loader import get_config_path, load_config, save_config

    config = load_config(get_config_path())
    current = config.brain_memory
    # Keep explicit null for retrieval_sessions: null means all sessions and
    # is different from an omitted field (leave the current selection intact).
    changes = payload.model_dump(exclude_unset=True)
    if "migration_sources" in changes:
        # Curated MEMORY.md is hidden after its first promotion, but remains a
        # configured source so the runtime can auto-sync later changes.
        automatic_sources = [
            source for source in current.migration_sources
            if source.casefold() in {"memory.md", "memory/memory.md"}
        ]
        changes["migration_sources"] = list(dict.fromkeys([
            *changes["migration_sources"], *automatic_sources,
        ]))
    if "enabled" not in changes:
        prospective = current.model_dump()
        prospective.update(changes)
        changes["enabled"] = any(
            prospective.get(key, False)
            for key in ("retrieval_enabled", "learning_enabled", "migration_enabled")
        )
    updated = type(current).model_validate({**current.model_dump(), **changes})
    config.brain_memory = updated
    save_config(config)
    request.app.state.config = config
    agent = getattr(request.app.state, "agent", None)
    if agent is not None and hasattr(agent, "_reload_config"):
        agent._reload_config()
    if agent is not None and hasattr(agent, "brain_memory_config"):
        # Provider reload can fail independently of a successfully saved Brain
        # setting. Keep the runtime in sync with the persisted settings anyway.
        if agent.brain_memory_config != updated:
            if runtime := getattr(agent, "brain_runtime", None):
                runtime.config_changed()
            agent.brain_memory_config = updated
            if context := getattr(agent, "context", None):
                context._brain_memory_config = updated
                context._brain_context = None
    return await memory_settings(request)


@router.post("/api/memory/migrate")
async def migrate_memory_sources(request: Request):
    _require_csrf(request)
    config = request.app.state.config.brain_memory
    if not config.migration_enabled:
        raise HTTPException(status_code=409, detail="Migration is disabled in Brain Graph settings")
    if not config.migration_stage_only and not config.learning_enabled:
        raise HTTPException(status_code=409, detail="Enable learning before promoting migration sources")
    if not config.enabled:
        raise HTTPException(status_code=409, detail="Save Graph Settings to enable Brain Memory before migration")
    runtime = None
    if not config.migration_stage_only:
        agent = getattr(request.app.state, "agent", None)
        runtime = getattr(agent, "brain_runtime", None)
        if runtime is None:
            raise HTTPException(status_code=409, detail="Learning runtime is unavailable; restart the bot")
        runtime_config = getattr(agent, "brain_memory_config", None)
        if runtime_config is None or not runtime_config.enabled or not runtime_config.learning_enabled:
            raise HTTPException(status_code=409, detail="Save Graph Settings to synchronize the learning worker")
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Migration request must be a JSON object")
        selected = body.get("source_refs", config.migration_sources)
        if not isinstance(selected, list) or not selected:
            raise HTTPException(status_code=400, detail="Select at least one migration source")
        if any(not isinstance(item, str) for item in selected):
            raise HTTPException(status_code=400, detail="Invalid source selection")
        # A configured session can also be discovered by the sessions directory.
        # Normalize duplicates before creating snapshots/jobs so one click cannot
        # enqueue the same source more than once.
        selected = list(dict.fromkeys(selected))
        if len(selected) > 50:
            raise HTTPException(status_code=400, detail="Select at most 50 migration sources")
        allowed = {
            item["source_ref"] for item in sources(request.app.state.config.workspace_path, config)
            if not item["migration_completed"]
        }
        if any(item not in allowed for item in selected):
            raise HTTPException(status_code=409, detail="A selected source is unavailable or already migrated; refresh the source list")
        effective = config.model_copy(update={"enabled": True})
        store = BrainStore(request.app.state.config.workspace_path, effective)
        # Always create an immutable snapshot first. With stage-only enabled it
        # remains pending; otherwise the bot's learning worker consumes it.
        results = [migrate_source(store, item, stage=True) for item in selected]
        batch_id = ""
        if not config.migration_stage_only:
            batch_id = migration_batch_id(results)
            write_migration_batch(store, results, batch_id)
            learning_job_ids = []
            for result in results:
                enqueue_jobs = getattr(runtime, "enqueue_migration_jobs", None)
                if enqueue_jobs is None:
                    learning_job_ids.append(await runtime.enqueue_migration(result, batch_id=batch_id))
                else:
                    learning_job_ids.extend(await enqueue_jobs(result, batch_id=batch_id))
            write_migration_batch(store, results, batch_id, learning_job_ids)
    except (DisabledError, ConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FormatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (BrainMemoryError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid migration request") from exc
    return {
        "results": [result.__dict__ for result in results],
        "queued": not config.migration_stage_only,
        "batch_id": batch_id,
        "deleted": False,
    }


@router.get("/api/memory/learning/status")
async def memory_learning_status(request: Request):
    agent = getattr(request.app.state, "agent", None)
    runtime = getattr(agent, "brain_runtime", None)
    worker = getattr(runtime, "worker", None)
    return learning_status(
        request.app.state.config.workspace_path,
        request.app.state.config.brain_memory,
        worker,
    )


@router.post("/api/memory/migration/undo")
async def undo_last_migration(request: Request):
    _require_csrf(request)
    try:
        body = await request.json()
        batch_id = body.get("batch_id") if isinstance(body, dict) else None
        if not isinstance(batch_id, str) or not batch_id.startswith("batch-"):
            raise ValueError("Invalid migration batch")
        return undo_migration_batch(
            request.app.state.config.workspace_path,
            request.app.state.config.brain_memory,
            batch_id,
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (BrainMemoryError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid migration batch") from exc


@router.post("/api/memory/graph/rebuild")
async def rebuild_memory_graph(
    request: Request,
    topic: str | None = Query(default=None, max_length=200),
    status: str = Query(default="active", max_length=20),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    query: str | None = Query(default=None, max_length=200),
    limit: int | None = Query(default=None, ge=1, le=5000),
):
    _require_csrf(request)
    # Graph build is bounded and derives only from Markdown. Auth middleware
    # protects this endpoint when dashboard password authentication is enabled.
    snapshot = _snapshot(
        request, topic=topic, status=status, min_confidence=min_confidence, query=query,
        limit=limit, refresh=True,
    )
    return _response(snapshot, request.app.state.config.brain_memory.max_response_bytes)


@router.get("/api/memory/facts/{fact_id}")
async def memory_fact(request: Request, fact_id: str):
    """Return one active fact addressed by its validated immutable ID."""
    config = request.app.state.config.brain_memory
    try:
        validated_id = TypeAdapter(FactId).validate_python(fact_id)
        store = BrainStore(request.app.state.config.workspace_path, config)
        fact = store.read_fact(validated_id)
    except (ValidationError, BrainMemoryError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Fact not found") from exc
    if fact.status != "active":
        raise HTTPException(status_code=404, detail="Fact not found")

    # A newer fact can supersede an older immutable record. Keep the old record
    # on disk for provenance but hide it from the default client-facing view.
    try:
        superseded = {
            old_id
            for path in sorted((store.root / "facts").glob("*.md"))
            if path.is_file() and not path.is_symlink()
            for old_id in store.read_fact(path.stem).supersedes
        }
    except (BrainMemoryError, OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid fact store") from exc
    if fact.id in superseded:
        raise HTTPException(status_code=404, detail="Fact not found")

    return JSONResponse({
        "id": fact.id,
        "title": fact.title,
        "body": fact.body,
        "type": fact.type,
        "topic": fact.topic,
        "entities": list(fact.entities),
        "tags": list(fact.tags),
        "confidence": fact.confidence,
        "status": fact.status,
        "created_at": fact.created_at,
        "source_session": fact.source_session,
        "supersedes": list(fact.supersedes),
        "citation": fact.source.model_dump(mode="json"),
    })
