"""Workspace file editor routes — SOUL.md, USER.md."""

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

router = APIRouter()

_EDITABLE_FILES = {
    "soul": "SOUL.md",
    "user": "USER.md",
}

# Default templates shipped with nanobot (for reference panel)
_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates"


def _get_workspace(request: Request) -> Path:
    """Resolve workspace path from app config."""
    return request.app.state.config.workspace_path


def _load_default_template(filename: str) -> str:
    """Load the default template content for a workspace file."""
    template_path = _TEMPLATES_DIR / filename
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")
    return ""


@router.get("/workspace/{file_key}", response_class=HTMLResponse)
async def workspace_editor(request: Request, file_key: str, saved: str | None = None):
    """Render the workspace file editor."""
    if file_key in ("soul", "user"):
        return RedirectResponse(url=f"/personality?tab={file_key}", status_code=302)

    filename = _EDITABLE_FILES.get(file_key)
    if not filename:
        return HTMLResponse("File not found", status_code=404)

    workspace = _get_workspace(request)
    file_path = workspace / filename
    content = ""
    if file_path.exists():
        content = file_path.read_text(encoding="utf-8")

    default_template = _load_default_template(filename)

    return request.app.state.templates.TemplateResponse(request, "workspace_editor.html", {"file_key": file_key,
        "filename": filename,
        "workspace": str(workspace),
        "content": content,
        "saved": saved == "1",
        "default_template": default_template})


@router.post("/workspace/{file_key}")
async def workspace_save(request: Request, file_key: str, content: str = Form("")):
    """Save workspace file content."""
    filename = _EDITABLE_FILES.get(file_key)
    if not filename:
        return HTMLResponse("File not found", status_code=404)

    workspace = _get_workspace(request)
    file_path = workspace / filename

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        # Normalize line endings: browser textarea sends \r\n
        content = content.replace('\r\n', '\n').replace('\r', '\n')
        file_path.write_text(content, encoding="utf-8")
        logger.info("[Web] Saved workspace file: {}", filename)

        # Also sync saved file into the active profile snapshot
        # so profile and workspace never diverge (fixes race condition with profile switch)
        try:
            import json
            profiles_dir = workspace / "profiles"
            pfile = profiles_dir / "profiles.json"
            if pfile.exists():
                data = json.loads(pfile.read_text(encoding="utf-8"))
                active_id = data.get("active")
                if active_id:
                    import shutil
                    from nanobot.web.routes.profiles import _PROFILE_FILES
                    subdir = _PROFILE_FILES.get(filename, "")
                    dst_dir = (profiles_dir / active_id / subdir) if subdir else (profiles_dir / active_id)
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file_path, dst_dir / filename)
                    logger.debug("[Web] Synced {} to active profile snapshot: {}", filename, active_id)
        except Exception as sync_err:
            logger.warning("[Web] Failed to sync {} to profile snapshot: {}", filename, sync_err)

        tpl = request.query_params.get("tpl", "")
        tpl_param = "&tpl=1" if tpl == "1" else ""
        return RedirectResponse(url=f"/workspace/{file_key}?saved=1{tpl_param}", status_code=302)
    except Exception as e:
        logger.error("[Web] Failed to save {}: {}", filename, e)
        default_template = _load_default_template(filename)
        return request.app.state.templates.TemplateResponse(request, "workspace_editor.html", {"file_key": file_key,
            "filename": filename,
            "workspace": str(workspace),
            "content": content,
            "saved": False,
            "default_template": default_template,
            "error": f"Failed to save: {e}"})
