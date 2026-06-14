"""Agentic Actions page — AGENTS.md + HEARTBEAT.md + TOOLS.md (all editable)."""

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

router = APIRouter()

_AGENTIC_FILES = {
    "agents": "AGENTS.md",
    "heartbeat": "HEARTBEAT.md",
    "tools": "TOOLS.md",
}

# Default templates shipped with nanobot (for reference panel)
_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates"


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


def _load_default_template(filename: str) -> str:
    """Load the default template content for a file."""
    template_path = _TEMPLATES_DIR / filename
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")
    return ""


@router.get("/agentic", response_class=HTMLResponse)
async def agentic_page(request: Request, saved: str | None = None, tab: str = "agents"):
    """Render the agentic actions page with AGENTS.md, HEARTBEAT.md, TOOLS.md tabs."""
    workspace = _get_workspace(request)

    agents = _read_file_info(workspace / "AGENTS.md")
    heartbeat = _read_file_info(workspace / "HEARTBEAT.md")
    tools = _read_file_info(workspace / "TOOLS.md")

    # Validate tab
    if tab not in _AGENTIC_FILES:
        tab = "agents"

    return request.app.state.templates.TemplateResponse(request, "agentic.html", {"agents": agents,
        "heartbeat": heartbeat,
        "tools": tools,
        "workspace": str(workspace),
        "saved": saved == "1",
        "saved_file": request.query_params.get("file", ""),
        "active_tab": tab,
        "default_agents": _load_default_template("AGENTS.md"),
        "default_heartbeat": _load_default_template("HEARTBEAT.md"),
        "default_tools": _load_default_template("TOOLS.md")})


@router.post("/agentic/{file_key}")
async def agentic_save(request: Request, file_key: str, content: str = Form("")):
    """Save an agentic file (AGENTS.md, HEARTBEAT.md, or TOOLS.md)."""
    filename = _AGENTIC_FILES.get(file_key)
    if not filename:
        return HTMLResponse("File not found", status_code=404)

    workspace = _get_workspace(request)
    file_path = workspace / filename

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        # Normalize line endings: browser textarea sends \r\n
        content = content.replace('\r\n', '\n').replace('\r', '\n')
        file_path.write_text(content, encoding="utf-8")
        logger.info("[Web] Saved {}", filename)
        return RedirectResponse(
            url=f"/agentic?saved=1&file={filename}&tab={file_key}",
            status_code=302,
        )
    except Exception as e:
        logger.error("[Web] Failed to save {}: {}", filename, e)
        # Re-read all files for template
        agents = _read_file_info(workspace / "AGENTS.md")
        heartbeat = _read_file_info(workspace / "HEARTBEAT.md")
        tools = _read_file_info(workspace / "TOOLS.md")
        # Override the failed file with submitted content
        override = {"content": content, "exists": True, "size": len(content.encode()), "modified": None}
        if file_key == "agents":
            agents = override
        elif file_key == "heartbeat":
            heartbeat = override
        elif file_key == "tools":
            tools = override

        return request.app.state.templates.TemplateResponse(request, "agentic.html", {"agents": agents,
            "heartbeat": heartbeat,
            "tools": tools,
            "workspace": str(workspace),
            "saved": False,
            "saved_file": "",
            "active_tab": file_key,
            "error": f"Failed to save {filename}: {e}"})
