"""Dashboard route — system overview."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Render the main dashboard page."""
    app_state = request.app.state

    config = app_state.config
    session_manager = app_state.session_manager

    # Gather system info
    defaults = config.agents.defaults
    effective_model = config.resolve_effective_model_config()
    sessions = session_manager.list_sessions() if session_manager else []

    # Count active channels (runtime status, not just config enabled)
    channel_manager = getattr(app_state, "channel_manager", None)
    active_channels = []
    if channel_manager:
        for ch_name, channel in channel_manager.channels.items():
            # For channels with get_status() (e.g. Zalo), check actual connection
            if hasattr(channel, "get_status"):
                status = channel.get_status()
                if status.get("status") == "connected":
                    active_channels.append(ch_name.capitalize())
            elif getattr(channel, "is_running", False):
                active_channels.append(ch_name.capitalize())

    # Active profile info
    workspace = config.workspace_path
    profile_name = None
    profile_preview = ""
    profile_created = ""
    profile_has_memory = False
    profiles_json = workspace / "profiles" / "profiles.json"
    if profiles_json.exists():
        try:
            import json
            pdata = json.loads(profiles_json.read_text(encoding="utf-8"))
            active_id = pdata.get("active")
            if active_id:
                for p in pdata.get("profiles", []):
                    if p["id"] == active_id:
                        profile_name = p.get("name", active_id)
                        profile_created = p.get("created", "")
                        # Read SOUL.md preview from profile dir
                        profile_dir = workspace / "profiles" / active_id
                        soul_path = profile_dir / "SOUL.md"
                        if soul_path.exists():
                            lines = soul_path.read_text(encoding="utf-8").splitlines()[:4]
                            profile_preview = "\n".join(lines)
                        profile_has_memory = (profile_dir / "memory" / "MEMORY.md").exists()
                        break
        except Exception:
            pass
    if not profile_name:
        # No profile system — read workspace SOUL.md directly
        profile_name = "Default"
        soul_path = workspace / "SOUL.md"
        if soul_path.exists():
            try:
                lines = soul_path.read_text(encoding="utf-8").splitlines()[:4]
                profile_preview = "\n".join(lines)
            except Exception:
                pass
        profile_has_memory = (workspace / "memory" / "MEMORY.md").exists()

    # Resolve active tools for display
    tools_config = config.tools
    preset = tools_config.tool_preset
    tool_groups = []
    if preset == "chatbot":
        tool_status = {"Message": True, "File": False, "Exec": False, "Web": True, "Spawn": False, "Cron": False, "MCP": True}
    elif preset == "coder":
        tool_status = {"Message": True, "File": True, "Exec": tools_config.sandbox_mode != "disabled", "Web": True, "Spawn": False, "Cron": False, "MCP": True}
    elif preset == "custom":
        tool_status = {
            "Message": True,
            "File": tools_config.enable_file_tools,
            "Exec": tools_config.sandbox_mode != "disabled",
            "Web": tools_config.enable_web_tools,
            "Spawn": tools_config.enable_spawn,
            "Cron": tools_config.enable_cron,
            "MCP": tools_config.enable_mcp,
        }
    else:  # developer
        tool_status = {"Message": True, "File": True, "Exec": tools_config.sandbox_mode != "disabled", "Web": True, "Spawn": True, "Cron": True, "MCP": True}

    agent = getattr(app_state, "agent", None)
    token_usage_tracker = getattr(agent, "token_usage", None)
    if token_usage_tracker is None:
        from nanobot.agent.token_usage import empty_token_usage_snapshot
        token_usage = empty_token_usage_snapshot()
    else:
        token_usage = token_usage_tracker.snapshot()

    context = {
        "request": request,
        "model": effective_model.model,
        "model_preset": effective_model.preset_name,
        "temperature": effective_model.temperature,
        "max_tokens": effective_model.max_tokens,
        "max_iterations": defaults.max_tool_iterations,
        "context_window_tokens": defaults.context_window_tokens,
        "reasoning_effort": defaults.reasoning_effort or "default",
        "sessions_count": len(sessions),
        "active_channels": active_channels,
        "provider": effective_model.provider,
        "tool_preset": preset,
        "tool_status": tool_status,
        "sandbox_mode": tools_config.sandbox_mode,
        "profile_name": profile_name,
        "profile_preview": profile_preview,
        "profile_created": profile_created,
        "profile_has_memory": profile_has_memory,
        "token_usage": token_usage,
    }

    return app_state.templates.TemplateResponse(request, "dashboard.html", context)


@router.post("/sessions/clear-all")
async def sessions_clear_all(request: Request):
    """Clear all chat sessions (soft reload). Forces bot to re-read SOUL.md on next message."""
    app_state = request.app.state
    session_manager = app_state.session_manager
    workspace = app_state.config.workspace_path

    cleared = 0
    # Clear session files on disk
    sessions_dir = workspace / "sessions"
    if sessions_dir.exists():
        for sf in sessions_dir.glob("*.jsonl"):
            try:
                sf.unlink()
                cleared += 1
            except OSError:
                pass

    # Clear in-memory cache
    if session_manager and hasattr(session_manager, '_cache'):
        session_manager._cache.clear()

    logger.info("[Web] Soft reload: cleared {} session(s)", cleared)
    return RedirectResponse(url="/sessions?reset=1", status_code=302)
