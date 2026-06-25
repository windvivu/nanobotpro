"""Dashboard routes for Adminbot web UI."""

from __future__ import annotations

import os
import threading

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from adminbot.app.web.viewmodels import build_bot_summary

router = APIRouter()


def _schedule_process_shutdown() -> None:
    threading.Timer(0.25, lambda: os._exit(0)).start()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    manager = request.app.state.manager
    templates = request.app.state.templates
    request_base_url = str(request.base_url)
    bots = [
        build_bot_summary(bot, request_base_url=request_base_url)
        for bot in manager.list_bots()
    ]
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


@router.post("/shutdown", response_class=HTMLResponse)
def shutdown_adminbot(request: Request):
    if getattr(request.app.state, "shutdown_disabled", False):
        return PlainTextResponse("Adminbot shutdown is disabled in this environment.", status_code=403)
    shutdown_callback = getattr(request.app.state, "shutdown_callback", _schedule_process_shutdown)
    shutdown_callback()
    return HTMLResponse(
        """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <meta content="width=device-width, initial-scale=1.0" name="viewport" />
    <title>Adminbot Shutting Down</title>
</head>
<body>
    <h1>Adminbot is shutting down</h1>
    <p>Managed bot processes are not stopped. Start Adminbot again from the terminal when needed.</p>
</body>
</html>
""".strip()
    )
