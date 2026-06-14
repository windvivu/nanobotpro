"""Cron jobs management routes."""

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from nanobot.cron.types import CronSchedule

router = APIRouter()


def _get_cron_service(request: Request):
    """Get CronService from the agent stored in app state."""
    agent = request.app.state.agent
    if agent and hasattr(agent, "cron_service") and agent.cron_service:
        return agent.cron_service
    return None


def _ms_to_iso(ms: int | None) -> str:
    """Convert milliseconds timestamp to human-readable ISO string."""
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _format_interval(schedule: CronSchedule) -> str:
    """Format schedule as human-readable string."""
    if schedule.kind == "every" and schedule.every_ms:
        secs = schedule.every_ms // 1000
        if secs < 60:
            return f"Every {secs}s"
        if secs < 3600:
            mins = secs // 60
            return f"Every {mins}m" if secs % 60 == 0 else f"Every {secs}s"
        if secs < 86400:
            hours = secs // 3600
            return f"Every {hours}h" if secs % 3600 == 0 else f"Every {secs}s"
        days = secs // 86400
        return f"Every {days}d" if secs % 86400 == 0 else f"Every {secs}s"
    if schedule.kind == "cron" and schedule.expr:
        tz_str = f" ({schedule.tz})" if schedule.tz else ""
        return f"Cron: {schedule.expr}{tz_str}"
    if schedule.kind == "at" and schedule.at_ms:
        return f"At: {_ms_to_iso(schedule.at_ms)}"
    return "Unknown"


def _job_to_dict(job) -> dict:
    """Serialize a CronJob to a JSON-safe dict."""
    return {
        "id": job.id,
        "name": job.name,
        "enabled": job.enabled,
        "schedule_kind": job.schedule.kind,
        "schedule_display": _format_interval(job.schedule),
        "schedule_every_ms": job.schedule.every_ms,
        "schedule_expr": job.schedule.expr,
        "schedule_tz": job.schedule.tz,
        "task": job.payload.message,
        "deliver": job.payload.deliver,
        "force_deliver": job.payload.force_deliver,
        "channel": job.payload.channel or "",
        "chat_id": job.payload.to or "",
        "last_run": _ms_to_iso(job.state.last_run_at_ms),
        "next_run": _ms_to_iso(job.state.next_run_at_ms),
        "last_status": job.state.last_status or "",
        "last_error": job.state.last_error or "",
        "created_at": _ms_to_iso(job.created_at_ms),
        "delete_after_run": job.delete_after_run,
    }


@router.get("/cron", response_class=HTMLResponse)
async def cron_page(request: Request):
    """Render the cron jobs management page."""
    cron = _get_cron_service(request)
    jobs = []
    status = {"enabled": False, "jobs": 0}
    if cron:
        jobs = [_job_to_dict(j) for j in cron.list_jobs(include_disabled=True)]
        status = cron.status()

    return request.app.state.templates.TemplateResponse(request, "cron.html", {"jobs": jobs,
        "total": len(jobs),
        "service_running": status.get("enabled", False)})


@router.get("/cron/list")
async def cron_list(request: Request):
    """JSON list of all cron jobs."""
    cron = _get_cron_service(request)
    if not cron:
        return JSONResponse({"jobs": [], "error": "Cron service not available"})
    jobs = [_job_to_dict(j) for j in cron.list_jobs(include_disabled=True)]
    return JSONResponse({"jobs": jobs})


@router.post("/cron/create")
async def cron_create(request: Request):
    """Create a new cron job."""
    cron = _get_cron_service(request)
    if not cron:
        return JSONResponse({"ok": False, "error": "Cron service not available"}, status_code=503)

    data = await request.json()
    name = (data.get("name") or "").strip()
    task = (data.get("task") or "").strip()
    schedule_kind = data.get("schedule_kind", "every")

    if not name or not task:
        return JSONResponse({"ok": False, "error": "Name and task are required"}, status_code=400)

    # Build schedule
    if schedule_kind == "every":
        interval_s = int(data.get("interval_seconds", 3600))
        if interval_s < 10:
            return JSONResponse({"ok": False, "error": "Minimum interval is 10 seconds"}, status_code=400)
        schedule = CronSchedule(kind="every", every_ms=interval_s * 1000)
    elif schedule_kind == "cron":
        expr = (data.get("cron_expr") or "").strip()
        tz = (data.get("tz") or "").strip() or None
        if not expr:
            return JSONResponse({"ok": False, "error": "Cron expression is required"}, status_code=400)
        schedule = CronSchedule(kind="cron", expr=expr, tz=tz)
    else:
        return JSONResponse({"ok": False, "error": f"Unsupported schedule kind: {schedule_kind}"}, status_code=400)

    channel = (data.get("channel") or "").strip() or None
    chat_id = (data.get("chat_id") or "").strip() or None
    deliver = bool(data.get("deliver", False))
    force_deliver = bool(data.get("force_deliver", False))

    try:
        job = cron.add_job(
            name=name,
            schedule=schedule,
            message=task,
            deliver=deliver,
            force_deliver=force_deliver,
            channel=channel,
            to=chat_id,
        )
        return JSONResponse({"ok": True, "job": _job_to_dict(job)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@router.put("/cron/{job_id}")
async def cron_update(request: Request, job_id: str):
    """Update an existing cron job."""
    cron = _get_cron_service(request)
    if not cron:
        return JSONResponse({"ok": False, "error": "Cron service not available"}, status_code=503)

    data = await request.json()
    kwargs = {}

    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "Name cannot be empty"}, status_code=400)
        kwargs["name"] = name
    if "task" in data:
        task = (data["task"] or "").strip()
        if not task:
            return JSONResponse({"ok": False, "error": "Task cannot be empty"}, status_code=400)
        kwargs["message"] = task
    if "deliver" in data:
        kwargs["deliver"] = bool(data["deliver"])
    if "force_deliver" in data:
        kwargs["force_deliver"] = bool(data["force_deliver"])
    if "channel" in data:
        kwargs["channel"] = (data["channel"] or "").strip() or None
    if "chat_id" in data:
        kwargs["to"] = (data["chat_id"] or "").strip() or None

    # Schedule update
    if "schedule_kind" in data:
        kind = data["schedule_kind"]
        if kind == "every":
            interval_s = int(data.get("interval_seconds", 3600))
            if interval_s < 10:
                return JSONResponse({"ok": False, "error": "Minimum interval is 10 seconds"}, status_code=400)
            kwargs["schedule"] = CronSchedule(kind="every", every_ms=interval_s * 1000)
        elif kind == "cron":
            expr = (data.get("cron_expr") or "").strip()
            if not expr:
                return JSONResponse({"ok": False, "error": "Cron expression required"}, status_code=400)
            tz = (data.get("tz") or "").strip() or None
            kwargs["schedule"] = CronSchedule(kind="cron", expr=expr, tz=tz)

    if not kwargs:
        return JSONResponse({"ok": False, "error": "No fields to update"}, status_code=400)

    try:
        result = cron.update_job(job_id, **kwargs)
        if result:
            return JSONResponse({"ok": True, "job": _job_to_dict(result)})
        return JSONResponse({"ok": False, "error": "Job not found"}, status_code=404)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@router.post("/cron/{job_id}/toggle")
async def cron_toggle(request: Request, job_id: str):
    """Toggle a job enabled/disabled."""
    cron = _get_cron_service(request)
    if not cron:
        return JSONResponse({"ok": False, "error": "Cron service not available"}, status_code=503)

    # Find current state
    jobs = cron.list_jobs(include_disabled=True)
    current = next((j for j in jobs if j.id == job_id), None)
    if not current:
        return JSONResponse({"ok": False, "error": "Job not found"}, status_code=404)

    result = cron.enable_job(job_id, enabled=not current.enabled)
    if result:
        return JSONResponse({"ok": True, "job": _job_to_dict(result)})
    return JSONResponse({"ok": False, "error": "Failed to toggle job"}, status_code=500)


@router.post("/cron/{job_id}/run")
async def cron_run(request: Request, job_id: str):
    """Force-run a job immediately."""
    cron = _get_cron_service(request)
    if not cron:
        return JSONResponse({"ok": False, "error": "Cron service not available"}, status_code=503)

    result = await cron.run_job(job_id, force=True)
    if result:
        # Refresh job data after run
        jobs = cron.list_jobs(include_disabled=True)
        updated = next((j for j in jobs if j.id == job_id), None)
        return JSONResponse({"ok": True, "job": _job_to_dict(updated) if updated else None})
    return JSONResponse({"ok": False, "error": "Job not found"}, status_code=404)


@router.delete("/cron/{job_id}")
async def cron_delete(request: Request, job_id: str):
    """Delete a cron job."""
    cron = _get_cron_service(request)
    if not cron:
        return JSONResponse({"ok": False, "error": "Cron service not available"}, status_code=503)

    removed = cron.remove_job(job_id)
    if removed:
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": "Job not found"}, status_code=404)
