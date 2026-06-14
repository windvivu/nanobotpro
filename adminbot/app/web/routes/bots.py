"""Bot routes for Adminbot web UI."""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from adminbot.app.web.viewmodels import build_bot_summary

router = APIRouter()


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "").lower()


def _action_success(request: Request, *, message: str, redirect_url: str):
    if _wants_json(request):
        return JSONResponse({"ok": True, "message": message})
    return _redirect(f"{redirect_url}?message={quote_plus(message)}")


def _action_error(request: Request, *, error: str, redirect_url: str):
    if _wants_json(request):
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    return _redirect(f"{redirect_url}?error={quote_plus(error)}")


@router.get("/bots/new", response_class=HTMLResponse)
def create_bot_page(request: Request):
    templates = request.app.state.templates
    manager = request.app.state.manager
    web_port = request.query_params.get("web_port")
    if not web_port:
        try:
            web_port = str(manager.suggest_web_port())
        except RuntimeError:
            web_port = "8899"
    context = {
        "request": request,
        "message": request.query_params.get("message", ""),
        "error": request.query_params.get("error", ""),
        "workspace": request.query_params.get("workspace", ""),
        "name": request.query_params.get("name", ""),
        "web_port": web_port,
    }
    return templates.TemplateResponse(request, "create_bot.html", context)


@router.post("/bots")
def create_bot(
    request: Request,
    workspace: str = Form(...),
    name: str = Form(""),
    web_port: int = Form(...),
):
    manager = request.app.state.manager
    try:
        bot = manager.create_bot(workspace, name or None, web_port)
        return _redirect(f"/bots/{bot.id}?message={quote_plus(f'Created bot {bot.name}.')}")
    except Exception as exc:
        return _redirect(
            "/bots/new"
            f"?error={quote_plus(str(exc))}"
            f"&workspace={quote_plus(workspace)}"
            f"&name={quote_plus(name)}"
            f"&web_port={web_port}"
        )


@router.get("/bots/{bot_id}", response_class=HTMLResponse)
def bot_detail(request: Request, bot_id: str):
    manager = request.app.state.manager
    templates = request.app.state.templates
    try:
        bot = manager.get_bot(bot_id)
    except RuntimeError as exc:
        return _redirect(f"/?error={quote_plus(str(exc))}")
    context = {
        "request": request,
        "bot": build_bot_summary(bot),
        "record": bot,
        "message": request.query_params.get("message", ""),
        "error": request.query_params.get("error", ""),
    }
    return templates.TemplateResponse(request, "bot_detail.html", context)


@router.post("/bots/{bot_id}/start")
def start_bot(request: Request, bot_id: str):
    manager = request.app.state.manager
    try:
        bot = manager.start_bot(bot_id)
        return _action_success(
            request,
            message=f"Started {bot.name}.",
            redirect_url=f"/bots/{bot.id}",
        )
    except Exception as exc:
        return _action_error(request, error=str(exc), redirect_url=f"/bots/{bot_id}")


@router.post("/bots/{bot_id}/stop")
def stop_bot(request: Request, bot_id: str):
    manager = request.app.state.manager
    try:
        bot = manager.stop_bot(bot_id)
        return _action_success(
            request,
            message=f"Stopped {bot.name}.",
            redirect_url=f"/bots/{bot.id}",
        )
    except Exception as exc:
        return _action_error(request, error=str(exc), redirect_url=f"/bots/{bot_id}")


@router.post("/bots/{bot_id}/restart")
def restart_bot(request: Request, bot_id: str):
    manager = request.app.state.manager
    try:
        bot = manager.restart_bot(bot_id)
        return _action_success(
            request,
            message=f"Restarted {bot.name}.",
            redirect_url=f"/bots/{bot.id}",
        )
    except Exception as exc:
        return _action_error(request, error=str(exc), redirect_url=f"/bots/{bot_id}")


@router.post("/bots/{bot_id}/delete")
def delete_bot(request: Request, bot_id: str, delete_workspace: bool = Form(False)):
    manager = request.app.state.manager
    try:
        bot = manager.delete_bot(bot_id, delete_workspace=delete_workspace)
        suffix = " and workspace files" if delete_workspace else ""
        return _redirect(f"/?message={quote_plus(f'Deleted bot {bot.name}{suffix}.')}")
    except Exception as exc:
        return _redirect(f"/bots/{bot_id}?error={quote_plus(str(exc))}")


@router.post("/bots/{bot_id}/shell")
def open_shell(request: Request, bot_id: str):
    manager = request.app.state.manager
    try:
        bot = manager.open_shell_for_bot(bot_id)
        return _redirect(f"/bots/{bot.id}?message={quote_plus(f'Opened shell for {bot.name}.')}")
    except Exception as exc:
        return _redirect(f"/bots/{bot_id}?error={quote_plus(str(exc))}")
