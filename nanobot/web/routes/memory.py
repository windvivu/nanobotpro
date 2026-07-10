"""Personality page — SOUL.md (editable) + MEMORY.md + history.jsonl viewer."""

import json
from datetime import datetime
from pathlib import Path

from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

router = APIRouter()

# Default templates shipped with nanobot (for reference panel)
_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates"


def _safe_remove(path: Path) -> None:
    """Remove a file if it exists, silently ignore if not."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _get_workspace(request: Request) -> Path:
    return request.app.state.config.workspace_path


def _read_file_info(file_path: Path) -> dict:
    """Read file content and metadata."""
    if not file_path.exists():
        return {"content": "", "size": 0, "modified": None, "exists": False}
    stat = file_path.stat()
    return {
        "content": file_path.read_text(encoding="utf-8"),
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "exists": True,
    }


def _load_default_template(filename: str, subdir: str = "") -> str:
    """Load the default template content for a file."""
    base = _TEMPLATES_DIR / subdir if subdir else _TEMPLATES_DIR
    template_path = base / filename
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")
    return ""


def _read_history_jsonl(memory_dir: Path) -> dict:
    """Read history.jsonl and format entries for display in the web UI."""
    history_file = memory_dir / "history.jsonl"
    if not history_file.exists() or history_file.stat().st_size == 0:
        return {"content": "", "size": 0, "modified": None, "exists": False}

    stat = history_file.stat()
    lines = []
    try:
        for raw in history_file.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
                ts = entry.get("timestamp", "")
                content = entry.get("content", "").strip()
                lines.append(f"[{ts}]\n{content}")
            except json.JSONDecodeError:
                lines.append(raw)
    except OSError:
        return {"content": "", "size": 0, "modified": None, "exists": False}

    return {
        "content": "\n\n---\n\n".join(lines),
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "exists": bool(lines),
    }


def _get_active_profile_name(workspace: Path) -> str:
    """Get the name of the currently active profile, or 'Default'."""
    pfile = workspace / "profiles" / "profiles.json"
    if not pfile.exists():
        return "Default"
    try:
        data = json.loads(pfile.read_text(encoding="utf-8"))
        active_id = data.get("active")
        if not active_id:
            return "Default"
        for p in data.get("profiles", []):
            if p["id"] == active_id:
                return p["name"]
    except (json.JSONDecodeError, OSError):
        pass
    return "Default"


@router.get("/personality", response_class=HTMLResponse)
async def personality_page(request: Request, saved: str | None = None, tab: str = "soul"):
    """Render the personality page with SOUL.md, USER.md, MEMORY.md, HISTORY.md tabs."""
    workspace = _get_workspace(request)
    memory_dir = workspace / "memory"
    if tab not in ("soul", "user", "memory", "history"):
        tab = "soul"

    soul = _read_file_info(workspace / "SOUL.md")
    user = _read_file_info(workspace / "USER.md")
    memory = _read_file_info(memory_dir / "MEMORY.md")
    history = _read_history_jsonl(memory_dir)

    # Read multi_message_enabled from config to initialize toggle correctly
    multi_message_enabled = getattr(
        getattr(request.app.state, "config", None), "chatbot", None
    )
    multi_message_enabled = getattr(multi_message_enabled, "multi_message_enabled", False)

    return request.app.state.templates.TemplateResponse(request, "personality.html", {"soul": soul,
        "user": user,
        "memory": memory,
        "history": history,
        "workspace": str(workspace),
        "saved": saved == "1",
        "active_tab": tab,
        "default_soul": _load_default_template("SOUL.md"),
        "default_user": _load_default_template("USER.md"),
        "default_memory": _load_default_template("MEMORY.md", subdir="memory"),
        "active_profile": _get_active_profile_name(workspace),
        "multi_message_enabled": multi_message_enabled})


@router.post("/personality")
async def personality_save(
    request: Request,
    file_key: str = Form("soul"),
    content: str = Form(""),
    multi_message_enabled: Optional[str] = Form(None),
):
    """Save editable personality files."""
    workspace = _get_workspace(request)
    file_map = {
        "soul": "SOUL.md",
        "user": "USER.md",
    }
    filename = file_map.get(file_key)
    if not filename:
        return HTMLResponse("File not found", status_code=404)
    file_path = workspace / filename

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        # Normalize line endings: browser textarea sends \r\n
        content = content.replace('\r\n', '\n').replace('\r', '\n')
        file_path.write_text(content, encoding="utf-8")
        logger.info("[Web] Saved {}", filename)

        if file_key == "soul":
            # Sync multi_message_enabled to config.json
            try:
                from nanobot.config.loader import load_config, save_config
                cfg = load_config()
                cfg.chatbot.multi_message_enabled = (multi_message_enabled == "true")
                save_config(cfg)
                # Also hot-reload into running app state so DeliveryPolicy picks it up
                if hasattr(request.app.state, "config"):
                    request.app.state.config.chatbot.multi_message_enabled = cfg.chatbot.multi_message_enabled
                logger.info("[Web] Synced multi_message_enabled={} to config", cfg.chatbot.multi_message_enabled)
            except Exception as cfg_err:
                logger.warning("[Web] Failed to sync multi_message_enabled to config: {}", cfg_err)

        # Sync to active profile snapshot to prevent divergence on profile switch
        try:
            import shutil
            pfile = workspace / "profiles" / "profiles.json"
            if pfile.exists():
                pdata = json.loads(pfile.read_text(encoding="utf-8"))
                active_id = pdata.get("active")
                if active_id:
                    dst_dir = workspace / "profiles" / active_id
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file_path, dst_dir / filename)
                    logger.debug("[Web] Synced {} to active profile: {}", filename, active_id)
        except Exception as sync_err:
            logger.warning("[Web] Failed to sync {} to profile: {}", filename, sync_err)

        tpl = request.query_params.get("tpl", "")
        tpl_param = "&tpl=1" if tpl == "1" else ""
        return RedirectResponse(url=f"/personality?saved=1&tab={file_key}{tpl_param}", status_code=302)
    except Exception as e:
        logger.error("[Web] Failed to save {}: {}", filename, e)
        workspace_path = workspace
        memory_dir = workspace_path / "memory"
        soul = _read_file_info(workspace_path / "SOUL.md")
        user = _read_file_info(workspace_path / "USER.md")
        if file_key == "soul":
            soul = {"content": content, "exists": True}
        elif file_key == "user":
            user = {"content": content, "exists": True}
        return request.app.state.templates.TemplateResponse(request, "personality.html", {"soul": soul,
            "user": user,
            "memory": _read_file_info(memory_dir / "MEMORY.md"),
            "history": _read_history_jsonl(memory_dir),
            "workspace": str(workspace_path),
            "saved": False,
            "active_tab": file_key,
            "default_soul": _load_default_template("SOUL.md"),
            "default_user": _load_default_template("USER.md"),
            "default_memory": _load_default_template("MEMORY.md", subdir="memory"),
            "active_profile": _get_active_profile_name(workspace_path),
            "multi_message_enabled": multi_message_enabled == "true",
            "error": f"Failed to save: {e}"})


@router.post("/personality/reset-memory")
async def personality_reset_memory(request: Request):
    """Reset MEMORY.md to default template and clear HISTORY.md in workspace."""
    import shutil

    workspace = _get_workspace(request)
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    # Load default MEMORY.md template
    default_memory = _load_default_template("MEMORY.md", subdir="memory")

    # Reset workspace memory
    (memory_dir / "MEMORY.md").write_text(default_memory, encoding="utf-8")
    (memory_dir / "history.jsonl").write_text("", encoding="utf-8")
    (memory_dir / "dream.jsonl").write_text("", encoding="utf-8")
    # Remove legacy file to prevent migration loop on restart
    _safe_remove(memory_dir / "HISTORY.md")
    # Remove cursor files so Dream starts fresh after reset
    _safe_remove(memory_dir / ".cursor")
    _safe_remove(memory_dir / ".dream_cursor")

    # Sync to active profile snapshot
    try:
        pfile = workspace / "profiles" / "profiles.json"
        if pfile.exists():
            pdata = json.loads(pfile.read_text(encoding="utf-8"))
            active_id = pdata.get("active")
            if active_id:
                prof_mem_dir = workspace / "profiles" / active_id / "memory"
                prof_mem_dir.mkdir(parents=True, exist_ok=True)
                (prof_mem_dir / "MEMORY.md").write_text(default_memory, encoding="utf-8")
                (prof_mem_dir / "history.jsonl").write_text("", encoding="utf-8")
                (prof_mem_dir / "dream.jsonl").write_text("", encoding="utf-8")
                _safe_remove(prof_mem_dir / "HISTORY.md")
                _safe_remove(prof_mem_dir / ".cursor")
                _safe_remove(prof_mem_dir / ".dream_cursor")
                logger.info("[Web] Reset memory synced to active profile: {}", active_id)
    except Exception as e:
        logger.warning("[Web] Failed to sync memory reset to profile: {}", e)

    logger.info("[Web] Reset MEMORY.md + history.jsonl")
    return RedirectResponse(url="/personality?saved=1&tab=memory", status_code=302)


# Legacy redirects
@router.get("/memory", response_class=HTMLResponse)
async def memory_redirect(request: Request):
    return RedirectResponse(url="/personality?tab=memory", status_code=302)


@router.get("/workspace/soul", response_class=HTMLResponse)
async def soul_redirect(request: Request):
    return RedirectResponse(url="/personality?tab=soul", status_code=302)


@router.get("/workspace/user", response_class=HTMLResponse)
async def user_redirect(request: Request):
    return RedirectResponse(url="/personality?tab=user", status_code=302)
