"""Authentication routes for Adminbot web UI."""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from adminbot.app.web.auth import SESSION_COOKIE

router = APIRouter()


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def _client_host(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    templates = request.app.state.templates
    context = {
        "request": request,
        "error": request.query_params.get("error", ""),
        "message": request.query_params.get("message", ""),
    }
    return templates.TemplateResponse(request, "login.html", context)


@router.post("/login")
def login(request: Request, username: str = Form("admin"), password: str = Form(...)):
    auth_store = request.app.state.auth_store
    limiter = request.app.state.login_rate_limiter
    client_host = _client_host(request)
    if limiter.is_locked(client_host, username):
        return _redirect("/login?error=Invalid+credentials+or+temporarily+locked")
    if username.strip().lower() != "admin" or not auth_store.verify(password):
        limiter.record_failure(client_host, username)
        return _redirect("/login?error=Invalid+credentials+or+temporarily+locked")
    limiter.clear(client_host, username)

    token = request.app.state.sessions.create()
    if auth_store.ensure_state().is_default_password:
        response = _redirect("/change-password")
    else:
        response = _redirect("/")
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=False,
    )
    return response


@router.get("/change-password", response_class=HTMLResponse)
def change_password_page(request: Request):
    if not getattr(request.state, "authenticated", False):
        return _redirect("/login")
    templates = request.app.state.templates
    context = {
        "request": request,
        "error": request.query_params.get("error", ""),
        "message": request.query_params.get("message", ""),
    }
    return templates.TemplateResponse(request, "change_password.html", context)


@router.post("/change-password")
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    if not getattr(request.state, "authenticated", False):
        return _redirect("/login")
    if new_password != confirm_password:
        return _redirect("/change-password?error=Passwords+do+not+match")
    try:
        request.app.state.auth_store.change_password(current_password, new_password)
    except ValueError as exc:
        return _redirect(f"/change-password?error={quote_plus(str(exc))}")
    token = request.app.state.sessions.rotate(request.cookies.get(SESSION_COOKIE))
    response = _redirect("/?message=Password+changed")
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=False,
    )
    return response


@router.post("/logout")
def logout(request: Request):
    request.app.state.sessions.remove(request.cookies.get(SESSION_COOKIE))
    response = _redirect("/login?message=Logged+out")
    response.delete_cookie(SESSION_COOKIE)
    return response
