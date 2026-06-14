"""Log routes for Adminbot web UI."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote_plus

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from adminbot.app.web.viewmodels import build_bot_summary

router = APIRouter()


def _redirect(url: str):
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url=url, status_code=303)


def _tail_lines(path: Path, limit: int) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-limit:]


def _safe_log_path(logs_dir: Path, bot_id: str, stream: str) -> Path:
    stream_name = "stderr" if stream == "stderr" else "stdout"
    root = logs_dir.resolve()
    path = (root / f"{bot_id}.{stream_name}.log").resolve()
    if not path.is_relative_to(root):
        raise ValueError("Invalid log path.")
    return path


@router.get("/bots/{bot_id}/logs", response_class=HTMLResponse)
def log_viewer(
    request: Request,
    bot_id: str,
    stream: str = Query("stdout"),
    tail: int = Query(200, ge=1, le=2000),
):
    manager = request.app.state.manager
    templates = request.app.state.templates
    paths = request.app.state.paths
    try:
        bot = manager.get_bot(bot_id)
    except RuntimeError as exc:
        return _redirect(f"/?error={quote_plus(str(exc))}")
    stream_name = "stderr" if stream == "stderr" else "stdout"
    log_path = _safe_log_path(paths.logs_dir, bot.id, stream_name)
    context = {
        "request": request,
        "bot": build_bot_summary(bot),
        "record": bot,
        "stream": stream_name,
        "tail": tail,
        "lines": _tail_lines(log_path, tail),
    }
    return templates.TemplateResponse(request, "log_viewer.html", context)


@router.get("/api/bots/{bot_id}/logs")
def log_lines(
    request: Request,
    bot_id: str,
    stream: str = Query("stdout"),
    tail: int = Query(200, ge=1, le=2000),
):
    manager = request.app.state.manager
    paths = request.app.state.paths
    try:
        bot = manager.get_bot(bot_id)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    stream_name = "stderr" if stream == "stderr" else "stdout"
    try:
        log_path = _safe_log_path(paths.logs_dir, bot.id, stream_name)
    except ValueError:
        return JSONResponse({"error": "Invalid log path"}, status_code=400)
    return JSONResponse(_tail_lines(log_path, tail))
