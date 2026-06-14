"""Dashboard routes for Adminbot web UI."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from adminbot.app.web.viewmodels import build_bot_summary

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    manager = request.app.state.manager
    templates = request.app.state.templates
    bots = [build_bot_summary(bot) for bot in manager.list_bots()]
    running_count = sum(1 for bot in bots if bot.status == "running")
    attention_count = sum(1 for bot in bots if bot.attention)
    never_started_count = sum(1 for bot in bots if bot.last_run_at == "-")
    context = {
        "request": request,
        "bots": bots,
        "running_count": running_count,
        "stopped_count": len(bots) - running_count,
        "attention_count": attention_count,
        "never_started_count": never_started_count,
        "message": request.query_params.get("message", ""),
        "error": request.query_params.get("error", ""),
    }
    return templates.TemplateResponse(request, "dashboard.html", context)
