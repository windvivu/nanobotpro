"""Soul Library routes for Adminbot web UI."""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from adminbot.app.soul_library import (
    create_custom_soul,
    delete_custom_soul,
    get_custom_soul,
    load_custom_souls,
    load_soul_templates,
    update_custom_soul,
)

router = APIRouter()


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def _custom_root(request: Request):
    return request.app.state.paths.runtime_root / "soul_library"


@router.get("/soul-library", response_class=HTMLResponse)
def soul_library(request: Request):
    templates = request.app.state.templates
    edit_id = request.query_params.get("edit", "").strip()
    edit_soul = None
    edit_error = ""
    if edit_id:
        try:
            edit_soul = get_custom_soul(_custom_root(request), edit_id)
        except Exception as exc:
            edit_error = str(exc)

    context = {
        "request": request,
        "custom_souls": load_custom_souls(_custom_root(request)),
        "built_in_souls": load_soul_templates(),
        "edit_soul": edit_soul,
        "message": request.query_params.get("message", ""),
        "error": request.query_params.get("error", "") or edit_error,
    }
    return templates.TemplateResponse(request, "soul_library.html", context)


@router.post("/soul-library/custom")
def create_soul(
    request: Request,
    title: str = Form(""),
    description: str = Form(""),
    content: str = Form(""),
):
    try:
        soul = create_custom_soul(
            _custom_root(request),
            title=title,
            description=description,
            content=content,
        )
        return _redirect(f"/soul-library?message={quote_plus(f'Created soul {soul.title}.')}")
    except Exception as exc:
        return _redirect(f"/soul-library?error={quote_plus(str(exc))}")


@router.post("/soul-library/custom/{soul_id}")
def update_soul(
    request: Request,
    soul_id: str,
    title: str = Form(""),
    description: str = Form(""),
    content: str = Form(""),
):
    try:
        soul = update_custom_soul(
            _custom_root(request),
            soul_id,
            title=title,
            description=description,
            content=content,
        )
        return _redirect(f"/soul-library?message={quote_plus(f'Updated soul {soul.title}.')}")
    except Exception as exc:
        return _redirect(f"/soul-library?edit={quote_plus(soul_id)}&error={quote_plus(str(exc))}")


@router.post("/soul-library/custom/{soul_id}/delete")
def delete_soul(request: Request, soul_id: str):
    try:
        delete_custom_soul(_custom_root(request), soul_id)
        return _redirect("/soul-library?message=Deleted+soul.")
    except Exception as exc:
        return _redirect(f"/soul-library?error={quote_plus(str(exc))}")
