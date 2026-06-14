"""Personality Profiles — create, manage, and switch between bot personalities."""

import json
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

router = APIRouter()


def _safe_remove(path: Path) -> None:
    """Remove a file if it exists, silently ignore if not."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass

# Files that belong to a profile
_PROFILE_FILES = {
    "SOUL.md": "",           # root of workspace
    "USER.md": "",           # root of workspace
    "MEMORY.md": "memory",   # inside memory/ subdir
    "HISTORY.md": "memory",  # legacy memory history
    "history.jsonl": "memory", # new jsonl history
    "dream.jsonl": "memory",   # async dream log
    ".cursor": "memory",       # history cursor
    ".dream_cursor": "memory", # dream processing cursor
}


def _get_workspace(request: Request) -> Path:
    return request.app.state.config.workspace_path


def _profiles_dir(workspace: Path) -> Path:
    return workspace / "profiles"


def _load_profiles_json(workspace: Path) -> dict:
    """Load profiles.json metadata."""
    pfile = _profiles_dir(workspace) / "profiles.json"
    if pfile.exists():
        try:
            return json.loads(pfile.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"active": None, "profiles": []}


def _save_profiles_json(workspace: Path, data: dict) -> None:
    """Save profiles.json metadata."""
    pdir = _profiles_dir(workspace)
    pdir.mkdir(parents=True, exist_ok=True)
    pfile = pdir / "profiles.json"
    pfile.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _save_workspace_to_profile(workspace: Path, profile_id: str) -> None:
    """Copy current workspace files into a profile directory."""
    profile_dir = _profiles_dir(workspace) / profile_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "memory").mkdir(parents=True, exist_ok=True)

    for filename, subdir in _PROFILE_FILES.items():
        src = (workspace / subdir / filename) if subdir else (workspace / filename)
        dst_dir = (profile_dir / subdir) if subdir else profile_dir
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / filename
        if src.exists():
            shutil.copy2(src, dst)
            logger.debug("[Profile] Saved {} -> {}", src, dst)
        elif dst.exists():
            # Source doesn't exist but profile has old copy — keep it
            logger.debug("[Profile] Skipped {} (not in workspace, keeping old copy)", filename)
        else:
            logger.debug("[Profile] Skipped {} (not found anywhere)", filename)


def _restore_profile_to_workspace(workspace: Path, profile_id: str) -> None:
    """Copy profile files back into the active workspace."""
    profile_dir = _profiles_dir(workspace) / profile_id
    logger.info("[Profile] Restoring profile '{}' -> workspace", profile_id)

    if not profile_dir.exists():
        logger.error("[Profile] Profile directory not found: {}", profile_dir)
        return

    for filename, subdir in _PROFILE_FILES.items():
        src_dir = (profile_dir / subdir) if subdir else profile_dir
        src = src_dir / filename
        dst_dir = (workspace / subdir) if subdir else workspace
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / filename
        if src.exists():
            try:
                shutil.copy2(src, dst)
                logger.info("[Profile] Restored {} -> {}", src.name, dst)
            except OSError as e:
                logger.error("[Profile] Failed to restore {}: {}", filename, e)
        else:
            # If profile doesn't have the file, we MUST remove it from workspace
            # so it doesn't inherit the previous profile's state (e.g. history)
            if dst.exists():
                try:
                    dst.unlink()
                    logger.info("[Profile] Cleared {} -> (not present in profile '{}')", filename, profile_id)
                except OSError as e:
                    logger.error("[Profile] Failed to clear {}: {}", filename, e)
            else:
                logger.debug("[Profile] {} not in profile '{}' and not in workspace", filename, profile_id)


def _slugify(name: str) -> str:
    """Create a filesystem-safe slug from a name."""
    import re
    slug = name.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = slug.strip('-')
    return slug or "unnamed"


def _read_preview(file_path: Path, lines: int = 3) -> str:
    """Read first N lines of a file for preview."""
    if not file_path.exists():
        return ""
    try:
        content = file_path.read_text(encoding="utf-8")
        return "\n".join(content.splitlines()[:lines])
    except OSError:
        return ""


def _get_profile_details(workspace: Path, profile: dict) -> dict:
    """Enrich profile metadata with file previews."""
    profile_dir = _profiles_dir(workspace) / profile["id"]
    soul_path = profile_dir / "SOUL.md"
    return {
        **profile,
        "soul_preview": _read_preview(soul_path, 4),
        "has_memory": (profile_dir / "memory" / "MEMORY.md").exists(),
        "has_history": (profile_dir / "memory" / "HISTORY.md").exists(),
    }


@router.get("/profiles", response_class=HTMLResponse)
async def profiles_page(request: Request, saved: str | None = None, action: str | None = None):
    """Render profiles management page."""
    workspace = _get_workspace(request)
    data = _load_profiles_json(workspace)

    profiles = [_get_profile_details(workspace, p) for p in data.get("profiles", [])]

    return request.app.state.templates.TemplateResponse(request, "profiles.html", {"profiles": profiles,
        "active_id": data.get("active"),
        "workspace": str(workspace),
        "saved": saved == "1",
        "action": action or ""})


@router.post("/profiles/create")
async def profiles_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    source: str = Form("current"),
):
    """Create a new profile."""
    workspace = _get_workspace(request)
    data = _load_profiles_json(workspace)

    profile_id = _slugify(name)

    # Ensure unique ID
    existing_ids = {p["id"] for p in data["profiles"]}
    base_id = profile_id
    counter = 1
    while profile_id in existing_ids:
        profile_id = f"{base_id}-{counter}"
        counter += 1

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_profile = {
        "id": profile_id,
        "name": name,
        "description": description,
        "created": now,
        "modified": now,
    }

    # Create profile directory
    profile_dir = _profiles_dir(workspace) / profile_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "memory").mkdir(parents=True, exist_ok=True)

    if source == "current":
        # Copy current workspace files
        _save_workspace_to_profile(workspace, profile_id)
    else:
        # Start from default templates
        templates_dir = Path(__file__).parent.parent.parent / "templates"
        for filename, subdir in _PROFILE_FILES.items():
            if filename == "SOUL.md":
                src = templates_dir / filename
            elif filename == "USER.md":
                src = templates_dir / filename
            elif filename == "MEMORY.md":
                src = templates_dir / "memory" / filename
            else:
                continue  # HISTORY.md has no template
            dst_dir = (profile_dir / subdir) if subdir else profile_dir
            if src.exists():
                shutil.copy2(src, dst_dir / filename)

    data["profiles"].append(new_profile)

    # If no active profile, set this one
    if not data.get("active"):
        data["active"] = profile_id

    _save_profiles_json(workspace, data)
    logger.info("[Web] Created profile: {} ({})", name, profile_id)
    return RedirectResponse(url="/profiles?saved=1&action=created", status_code=302)


@router.post("/profiles/switch")
async def profiles_switch(request: Request, profile_id: str = Form(...)):
    """Switch to a different profile."""
    workspace = _get_workspace(request)
    data = _load_profiles_json(workspace)

    # Validate target exists
    target = None
    for p in data["profiles"]:
        if p["id"] == profile_id:
            target = p
            break

    if not target:
        return RedirectResponse(url="/profiles", status_code=302)

    # 1. Save current workspace to current active profile
    current_active = data.get("active")
    if current_active and current_active != profile_id:
        logger.info("[Profile/Switch] Saving workspace -> profile: {}", current_active)
        _save_workspace_to_profile(workspace, current_active)
        # Update modified time
        for p in data["profiles"]:
            if p["id"] == current_active:
                p["modified"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 2. Restore target profile to workspace
    logger.info("[Profile/Switch] Restoring profile -> workspace: {}", profile_id)
    _restore_profile_to_workspace(workspace, profile_id)

    # 3. Update active
    data["active"] = profile_id
    _save_profiles_json(workspace, data)

    # 4. Clear all chat sessions so bot uses new identity from SOUL.md
    #    Old sessions contain messages with the previous profile's personality
    sessions_dir = workspace / "sessions"
    if sessions_dir.exists():
        cleared = 0
        for sf in sessions_dir.glob("*.jsonl"):
            try:
                sf.unlink()
                cleared += 1
            except OSError:
                pass
        logger.info("[Profile/Switch] Cleared {} session file(s)", cleared)

    # Also invalidate in-memory session cache
    try:
        sm = request.app.state.session_manager
        if hasattr(sm, '_cache'):
            sm._cache.clear()
            logger.info("[Profile/Switch] Cleared in-memory session cache")
    except (AttributeError, Exception) as e:
        logger.debug("[Profile/Switch] Could not clear in-memory cache: {}", e)

    logger.info("[Web] Switched to profile: {} ({})", target["name"], profile_id)
    return RedirectResponse(url="/profiles?saved=1&action=switched", status_code=302)


@router.post("/profiles/delete")
async def profiles_delete(request: Request, profile_id: str = Form(...)):
    """Delete a profile."""
    workspace = _get_workspace(request)
    data = _load_profiles_json(workspace)

    # Remove from list
    data["profiles"] = [p for p in data["profiles"] if p["id"] != profile_id]

    # Clear active if deleted
    if data.get("active") == profile_id:
        data["active"] = data["profiles"][0]["id"] if data["profiles"] else None

    # Remove directory
    profile_dir = _profiles_dir(workspace) / profile_id
    if profile_dir.exists():
        shutil.rmtree(profile_dir)

    _save_profiles_json(workspace, data)
    logger.info("[Web] Deleted profile: {}", profile_id)
    return RedirectResponse(url="/profiles?saved=1&action=deleted", status_code=302)


@router.post("/profiles/rename")
async def profiles_rename(
    request: Request,
    profile_id: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
):
    """Rename a profile."""
    workspace = _get_workspace(request)
    data = _load_profiles_json(workspace)

    for p in data["profiles"]:
        if p["id"] == profile_id:
            p["name"] = name
            p["description"] = description
            p["modified"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            break

    _save_profiles_json(workspace, data)
    logger.info("[Web] Renamed profile: {} -> {}", profile_id, name)
    return RedirectResponse(url="/profiles?saved=1&action=renamed", status_code=302)


@router.get("/profiles/{profile_id}/edit", response_class=HTMLResponse)
async def profile_edit_page(request: Request, profile_id: str, tab: str = "soul", saved: str | None = None):
    """Edit a profile's files (SOUL.md, USER.md, MEMORY.md, HISTORY.md)."""
    workspace = _get_workspace(request)
    data = _load_profiles_json(workspace)

    # Find profile
    profile = None
    for p in data["profiles"]:
        if p["id"] == profile_id:
            profile = p
            break
    if not profile:
        return RedirectResponse(url="/profiles", status_code=302)

    profile_dir = _profiles_dir(workspace) / profile_id

    # Read all 4 files
    def _read(fpath: Path) -> dict:
        if fpath.exists():
            content = fpath.read_text(encoding="utf-8")
            stat = fpath.stat()
            return {"content": content, "size": stat.st_size, "exists": True}
        return {"content": "", "size": 0, "exists": False}

    soul = _read(profile_dir / "SOUL.md")
    user = _read(profile_dir / "USER.md")
    memory = _read(profile_dir / "memory" / "MEMORY.md")
    history = _read(profile_dir / "memory" / "HISTORY.md")

    # Load default templates for reference
    templates_dir = Path(__file__).parent.parent.parent / "templates"

    def _load_tpl(filename: str, subdir: str = "") -> str:
        base = templates_dir / subdir if subdir else templates_dir
        p = base / filename
        return p.read_text(encoding="utf-8") if p.exists() else ""

    return request.app.state.templates.TemplateResponse(request, "profile_edit.html", {"profile": profile,
        "profile_id": profile_id,
        "active_id": data.get("active"),
        "soul": soul,
        "user": user,
        "memory": memory,
        "history": history,
        "active_tab": tab,
        "saved": saved == "1",
        "default_soul": _load_tpl("SOUL.md"),
        "default_user": _load_tpl("USER.md"),
        "default_memory": _load_tpl("MEMORY.md", subdir="memory")})


@router.post("/profiles/{profile_id}/edit")
async def profile_edit_save(
    request: Request,
    profile_id: str,
    tab: str = Form("soul"),
    content: str = Form(""),
):
    """Save an edited profile file."""
    workspace = _get_workspace(request)
    data = _load_profiles_json(workspace)

    # Validate profile exists
    found = any(p["id"] == profile_id for p in data["profiles"])
    if not found:
        return RedirectResponse(url="/profiles", status_code=302)

    profile_dir = _profiles_dir(workspace) / profile_id

    # Map tab to file path
    file_map = {
        "soul": profile_dir / "SOUL.md",
        "user": profile_dir / "USER.md",
        "memory": profile_dir / "memory" / "MEMORY.md",
        "history": profile_dir / "memory" / "HISTORY.md",
    }

    file_path = file_map.get(tab)
    if not file_path:
        return RedirectResponse(url=f"/profiles/{profile_id}/edit?tab={tab}", status_code=302)

    # Save
    file_path.parent.mkdir(parents=True, exist_ok=True)
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    file_path.write_text(content, encoding="utf-8")

    # Update modified timestamp
    for p in data["profiles"]:
        if p["id"] == profile_id:
            p["modified"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            break
    _save_profiles_json(workspace, data)

    logger.info("[Web] Saved profile file: {}/{}", profile_id, tab)
    tpl = request.query_params.get("tpl", "")
    tpl_param = f"&tpl=1" if tpl == "1" else ""
    return RedirectResponse(url=f"/profiles/{profile_id}/edit?tab={tab}&saved=1{tpl_param}", status_code=302)


@router.post("/profiles/{profile_id}/reset-memory")
async def profile_reset_memory(request: Request, profile_id: str):
    """Reset MEMORY.md to default template and clear HISTORY.md for a profile."""
    workspace = _get_workspace(request)
    data = _load_profiles_json(workspace)

    # Validate profile exists
    found = any(p["id"] == profile_id for p in data["profiles"])
    if not found:
        return RedirectResponse(url="/profiles", status_code=302)

    # Load default MEMORY.md template
    templates_dir = Path(__file__).parent.parent.parent / "templates"
    tpl_path = templates_dir / "memory" / "MEMORY.md"
    default_memory = tpl_path.read_text(encoding="utf-8") if tpl_path.exists() else ""

    profile_dir = _profiles_dir(workspace) / profile_id
    memory_dir = profile_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    # Reset memory to template, clear history
    (memory_dir / "MEMORY.md").write_text(default_memory, encoding="utf-8")
    (memory_dir / "history.jsonl").write_text("", encoding="utf-8")
    (memory_dir / "dream.jsonl").write_text("", encoding="utf-8")
    # Remove legacy file to prevent migration loop on restart
    _safe_remove(memory_dir / "HISTORY.md")
    # Remove cursor files so Dream starts fresh after reset
    _safe_remove(memory_dir / ".cursor")
    _safe_remove(memory_dir / ".dream_cursor")

    # If this is the active profile, also sync to workspace
    if data.get("active") == profile_id:
        ws_memory_dir = workspace / "memory"
        ws_memory_dir.mkdir(parents=True, exist_ok=True)
        (ws_memory_dir / "MEMORY.md").write_text(default_memory, encoding="utf-8")
        (ws_memory_dir / "history.jsonl").write_text("", encoding="utf-8")
        (ws_memory_dir / "dream.jsonl").write_text("", encoding="utf-8")
        _safe_remove(ws_memory_dir / "HISTORY.md")
        _safe_remove(ws_memory_dir / ".cursor")
        _safe_remove(ws_memory_dir / ".dream_cursor")
        logger.info("[Web] Reset memory for active profile {}: workspace synced", profile_id)
    else:
        logger.info("[Web] Reset memory for profile {}", profile_id)

    return RedirectResponse(url=f"/profiles/{profile_id}/edit?tab=memory&saved=1", status_code=302)
