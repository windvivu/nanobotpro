"""API routes for real-time data endpoints."""

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from loguru import logger
from starlette.responses import StreamingResponse

router = APIRouter()

_start_time = time.time()

# ── Fleet Claim State (runtime, not persisted) ─────────────────
_fleet_claim: dict = {
    "fleet_id": "",       # Which fleet claimed this bot
    "claimed_by": "",     # IP of the fleet-manager that claimed
    "claimed_at": "",     # ISO timestamp of claim
}

_FLEET_MCP_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]{1,64}$")


def _fleet_mcp_name(value: object) -> str:
    name = "fleet" if value is None else str(value).strip()
    if not name:
        name = "fleet"
    if not _FLEET_MCP_NAME_RE.fullmatch(name):
        raise ValueError("Invalid MCP server name")
    return name


@router.get("/api/password-status")
async def password_status(request: Request):
    """Whether the web password is still the generated default. Needs the dashboard login and never
    returns the password: base.html shows the warning on every page, the login page included (custom)."""
    pwd = getattr(getattr(request.app.state.config.gateway, "web", None), "password", "") or ""
    return JSONResponse({"is_default": pwd.startswith("nanobot@")})


@router.get("/healthz")
async def healthz():
    """Lightweight liveness probe for browser restart progress pages."""
    return JSONResponse({"ok": True})


@router.get("/api/health")
async def health(request: Request):
    """Health check endpoint for fleet monitoring.

    Always requires HTTP Basic Auth (gateway.web.password).
    Rate limited: 5 failures per 5 min → block 5 min.
    """
    try:
        from nanobot.web.auth import (
            is_rate_limited,
            record_auth_failure,
            record_auth_success,
            verify_basic_auth,
        )

        client_ip = request.client.host if request.client else "unknown"

        # Check rate limit first
        if is_rate_limited(client_ip):
            return JSONResponse(
                {"status": "error", "error": "Too many failed attempts — try again later"},
                status_code=429,
                headers={"Retry-After": "300"},
            )

        config = request.app.state.config
        gateway = config.gateway
        stored_password = getattr(config.gateway.web, "password", None) or getattr(gateway, "password", None)

        # Bot must have a password configured
        if not stored_password:
            return JSONResponse(
                {"status": "error", "error": "Bot password not configured (set gateway.web.password)"},
                status_code=401,
                headers={"WWW-Authenticate": "Basic realm=\"Nanobot Fleet\""},
            )

        auth_header = request.headers.get("Authorization")
        if not verify_basic_auth(auth_header, stored_password):
            record_auth_failure(client_ip)
            return JSONResponse(
                {"status": "error", "error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Basic realm=\"Nanobot Fleet\""},
            )

        # Auth OK — clear failure log
        record_auth_success(client_ip)

        bot_id = gateway.bot_id or "default"
        uptime_seconds = int(time.time() - _start_time)

        channels = []
        channel_manager = getattr(request.app.state, "channel_manager", None)
        if channel_manager:
            channels = list(channel_manager.channels.keys())

        model = getattr(getattr(config.agents, "defaults", None), "model", "unknown")
        provider = config.get_provider_name() if hasattr(config, "get_provider_name") else ""
        # Avoid duplicate prefix: model may already be "gemini/gemini-2.5-flash"
        if provider and not model.startswith(f"{provider}/"):
            model_label = f"{provider}/{model}"
        else:
            model_label = model

        return JSONResponse({
            "status": "ok",
            "bot_id": bot_id,
            "fleet_id": _fleet_claim["fleet_id"] or gateway.fleet_id or "",
            "fleet_claimed_by": _fleet_claim["claimed_by"],
            "fleet_claimed_at": _fleet_claim["claimed_at"],
            "model": model_label,
            "uptime": uptime_seconds,
            "channels": channels,
            "web_port": gateway.web.port,
        })
    except Exception as e:
        logger.error("[API] Health check error: {}", e)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


def _verify_fleet_auth(request: Request) -> JSONResponse | None:
    """Shared auth check for fleet claim/release endpoints.

    Returns error JSONResponse if auth fails, None if OK.
    """
    from nanobot.web.auth import (
        is_rate_limited,
        record_auth_failure,
        record_auth_success,
        verify_basic_auth,
    )

    client_ip = request.client.host if request.client else "unknown"
    if is_rate_limited(client_ip):
        return JSONResponse(
            {"status": "error", "error": "Rate limited"},
            status_code=429, headers={"Retry-After": "300"},
        )

    config = request.app.state.config
    stored_password = getattr(config.gateway.web, "password", None)
    if not stored_password:
        return JSONResponse(
            {"status": "error", "error": "Bot password not configured"},
            status_code=401,
        )

    auth_header = request.headers.get("Authorization")
    if not verify_basic_auth(auth_header, stored_password):
        record_auth_failure(client_ip)
        return JSONResponse(
            {"status": "error", "error": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Basic realm=\"Nanobot Fleet\""},
        )

    record_auth_success(client_ip)
    return None


@router.post("/api/fleet/claim")
async def fleet_claim(request: Request):
    """Fleet-manager claims this bot. Stores fleet_id + caller IP in runtime.

    - If unclaimed: accept, store fleet_id + IP
    - If same fleet_id: idempotent, update IP/timestamp
    - If different fleet_id: reject (409 Conflict)
    """
    global _fleet_claim

    auth_err = _verify_fleet_auth(request)
    if auth_err:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    fleet_id = (body.get("fleet_id", "") or "").strip()
    if not fleet_id:
        return JSONResponse({"ok": False, "error": "fleet_id is required"}, status_code=400)

    client_ip = request.client.host if request.client else "unknown"
    current = _fleet_claim["fleet_id"]

    # Also check config-level lock
    config_fleet = getattr(request.app.state.config.gateway, "fleet_id", "")
    if config_fleet and config_fleet != fleet_id:
        return JSONResponse({
            "ok": False,
            "error": f"Bot is locked to fleet '{config_fleet}' via config. Cannot claim for '{fleet_id}'.",
        }, status_code=409)

    if current and current != fleet_id:
        return JSONResponse({
            "ok": False,
            "error": f"Bot already claimed by fleet '{current}' (from {_fleet_claim['claimed_by']}). Release first.",
            "claimed_by_fleet": current,
            "claimed_by_ip": _fleet_claim["claimed_by"],
            "claimed_at": _fleet_claim["claimed_at"],
        }, status_code=409)

    # Accept claim (or update existing)
    _fleet_claim = {
        "fleet_id": fleet_id,
        "claimed_by": client_ip,
        "claimed_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("[Fleet] Bot claimed by fleet '{}' from {}", fleet_id, client_ip)
    return JSONResponse({"ok": True, "fleet_id": fleet_id, "claimed_by": client_ip})


@router.post("/api/fleet/release")
async def fleet_release(request: Request):
    """Release this bot from its fleet. Only the claiming fleet can release."""
    global _fleet_claim

    auth_err = _verify_fleet_auth(request)
    if auth_err:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    fleet_id = (body.get("fleet_id", "") or "").strip()
    if not fleet_id:
        return JSONResponse({"ok": False, "error": "fleet_id is required"}, status_code=400)

    # Check both runtime claim AND config fleet_id
    current_runtime = _fleet_claim["fleet_id"]
    config_fleet = getattr(request.app.state.config.gateway, "fleet_id", "")
    current = current_runtime or config_fleet

    if not current:
        return JSONResponse({"ok": True, "message": "Bot is not claimed by any fleet"})

    if current != fleet_id:
        return JSONResponse({
            "ok": False,
            "error": f"Bot is claimed by fleet '{current}', not '{fleet_id}'. Only '{current}' can release.",
        }, status_code=403)

    # Clear runtime claim
    old_claim = _fleet_claim.copy()
    _fleet_claim = {"fleet_id": "", "claimed_by": "", "claimed_at": ""}

    # Also clear config-level fleet_id (runtime only, does NOT rewrite config file)
    if config_fleet == fleet_id:
        try:
            request.app.state.config.gateway.fleet_id = ""
            logger.info("[Fleet] Cleared gateway.fleet_id from runtime config")
        except Exception as e:
            logger.warning("[Fleet] Could not clear gateway.fleet_id: {}", e)

    logger.info("[Fleet] Bot released from fleet '{}' (was claimed by {})", fleet_id, old_claim.get("claimed_by", "?"))
    return JSONResponse({"ok": True})


@router.get("/api/fleet/status")
async def fleet_status(request: Request):
    """Return fleet claim status for the bot's own dashboard (session-auth, no Basic Auth)."""
    return JSONResponse(_fleet_claim)

    # NOTE: force-release endpoint disabled for now — will enable later.
    # @router.post("/api/fleet/force-release")
    # async def fleet_force_release(request: Request):
    #     """Force-release this bot from its fleet (session-auth, bot owner only).
    #
    #     Used when fleet-manager's release POST fails due to network issues.
    #     No fleet_id required — clears everything unconditionally.
    #     """
    #     global _fleet_claim
    #
    #     old = _fleet_claim.copy()
    #     if not old["fleet_id"]:
    #         return JSONResponse({"ok": True, "message": "Bot is not claimed by any fleet"})
    #
    #     _fleet_claim = {"fleet_id": "", "claimed_by": "", "claimed_at": ""}
    #
    #     # Also clear config-level fleet_id from runtime
    #     try:
    #         config_fleet = getattr(request.app.state.config.gateway, "fleet_id", "")
    #         if config_fleet:
    #             request.app.state.config.gateway.fleet_id = ""
    #     except Exception:
    #         pass
    #
    #     logger.info(
    #         "[Fleet] FORCE-RELEASED from fleet '{}' (was claimed by {})",
    #         old["fleet_id"], old.get("claimed_by", "?"),
    #     )
    #     return JSONResponse({"ok": True, "released_from": old["fleet_id"]})


@router.get("/api/token-usage")
async def token_usage(request: Request):
    """Return in-memory token usage stats for the authenticated dashboard."""
    from nanobot.agent.token_usage import empty_token_usage_snapshot

    agent = getattr(request.app.state, "agent", None)
    tracker = getattr(agent, "token_usage", None)
    if tracker is None:
        stats = empty_token_usage_snapshot()
    else:
        stats = tracker.snapshot()
    return JSONResponse({"ok": True, "stats": stats})


@router.post("/api/fleet/message")
async def fleet_message(request: Request):
    """Receive a message from another bot in the fleet.

    Auth: Basic Auth (same as /api/health).
    Rate limited: 5 failures per 5 min per IP.
    Body JSON: {
        "from_bot": "alpha",
        "content": "message text",
        "session_key": "fleet:alpha"  (optional)
    }
    """
    try:
        from nanobot.web.auth import (
            is_rate_limited,
            record_auth_failure,
            record_auth_success,
            verify_basic_auth,
        )

        client_ip = request.client.host if request.client else "unknown"

        if is_rate_limited(client_ip):
            return JSONResponse(
                {"ok": False, "error": "Too many failed attempts — try again later"},
                status_code=429,
                headers={"Retry-After": "300"},
            )

        config = request.app.state.config
        stored_password = getattr(config.gateway.web, "password", None) or getattr(config.gateway, "password", None)

        if not stored_password:
            return JSONResponse(
                {"ok": False, "error": "Bot password not configured"},
                status_code=401,
            )

        auth_header = request.headers.get("Authorization")
        if not verify_basic_auth(auth_header, stored_password):
            record_auth_failure(client_ip)
            return JSONResponse(
                {"ok": False, "error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Basic realm=\"Nanobot Fleet\""},
            )

        record_auth_success(client_ip)

        # Parse body
        body = await request.json()
        from_bot = body.get("from_bot", "unknown")
        content = body.get("content", "").strip()
        session_key = body.get("session_key", f"fleet:{from_bot}")

        if not content:
            return JSONResponse({"ok": False, "error": "Empty message"}, status_code=400)

        # Process via agent
        agent = getattr(request.app.state, "agent", None)
        if not agent:
            return JSONResponse({"ok": False, "error": "Agent not available"}, status_code=503)

        logger.info("[Fleet] Message from '{}': {}", from_bot, content[:100])

        try:
            resp = await asyncio.shield(
                agent.process_direct(
                    content=content,
                    session_key=session_key,
                    channel="fleet",
                    chat_id=from_bot,
                )
            )
        except asyncio.CancelledError:
            logger.warning(
                "[Fleet] Message request cancelled while agent was processing '{}'; returning controlled 499",
                from_bot,
            )
            return JSONResponse(
                {"ok": False, "error": "Fleet message request cancelled"},
                status_code=499,
            )
        response = resp.content if resp else ""

        logger.info("[Fleet] Reply to '{}': {}", from_bot, response[:100] if response else "(empty)")

        return JSONResponse({"ok": True, "response": response})

    except Exception as e:
        logger.error("[API] Fleet message error: {}", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/api/prompt")
async def get_prompt(request: Request, since: float = 0):
    """Return prompt.md content with smart polling (304 if unchanged).

    Query params:
        since: Last known mtime (float). If file hasn't changed since, returns 304.
    """
    try:
        workspace: Path = request.app.state.config.workspace_path
        prompt_path = workspace / "prompt.md"

        if not prompt_path.exists():
            return JSONResponse({"content": "", "mtime": 0, "exists": False})

        mtime = os.path.getmtime(prompt_path)

        # Smart polling: return 304 if file hasn't changed
        if since and mtime <= since:
            return Response(status_code=304)

        content = prompt_path.read_text(encoding="utf-8")
        return JSONResponse({
            "content": content,
            "mtime": mtime,
            "exists": True,
            "size": len(content),
        })
    except Exception as e:
        logger.error("[API] Error reading prompt.md: {}", e)
        return JSONResponse({"content": "", "mtime": 0, "exists": False, "error": str(e)})


@router.get("/api/logs/stream")
async def logs_stream(request: Request):
    """SSE endpoint for real-time log streaming.

    Sends batched log entries every 300ms to avoid overwhelming the client.
    """
    from nanobot.web.log_sink import exec_buffer, log_buffer

    async def event_generator():
        last_id = log_buffer.latest_id  # Start from current position
        last_exec_id = exec_buffer.latest_id

        # Send initial batch (last 50 entries for context)
        initial = log_buffer.get_all()[-50:]
        if initial:
            yield f"event: logs\ndata: {json.dumps(initial, ensure_ascii=False)}\n\n"

        # Send initial exec entries
        initial_exec = exec_buffer.get_all()[-20:]
        if initial_exec:
            yield f"event: exec\ndata: {json.dumps(initial_exec, ensure_ascii=False)}\n\n"

        while True:
            if await request.is_disconnected():
                break

            entries = log_buffer.get_since(last_id)
            if entries:
                last_id = entries[-1]["id"]
                yield f"event: logs\ndata: {json.dumps(entries, ensure_ascii=False)}\n\n"

            exec_entries = exec_buffer.get_since(last_exec_id)
            if exec_entries:
                last_exec_id = exec_entries[-1]["id"]
                yield f"event: exec\ndata: {json.dumps(exec_entries, ensure_ascii=False)}\n\n"

            await asyncio.sleep(0.3)  # Throttle: batch every 300ms

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/fleet/config-mcp")
async def fleet_config_mcp(request: Request):
    """Push a Fleet MCP server config into this bot's config.json.

    Called by Fleet Manager after claiming a bot, so the bot gains
    access to fleet workspace tools without any manual setup.

    Auth: Basic Auth (same as /api/health).
    Body JSON: {
        "name": "fleet",        (optional, default "fleet")
        "type": "streamableHttp",
        "url": "http://fleet-mgr:9000/mcp",
        "headers": {            (optional)
            "X-Bot-ID": "alpha",
            "X-Fleet-ID": "myfleet01"
        },
        "tool_timeout": 30,     (optional, default 30)
        "enabled_tools": ["*"]  (optional, default all)
    }
    Response: { "ok": true, "name": "<name>", "restart_required": bool, "hot_applied": bool }
    """
    from nanobot.config.loader import get_config_path, load_config, save_config
    from nanobot.config.schema import MCPServerConfig

    auth_err = _verify_fleet_auth(request)
    if auth_err:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    url = (body.get("url") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "url is required"}, status_code=400)
    try:
        name = _fleet_mcp_name(body.get("name"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    mcp_type = (body.get("type") or "streamableHttp").strip()
    headers = body.get("headers") or {}
    tool_timeout = int(body.get("tool_timeout", 30))
    enabled_tools = body.get("enabled_tools", ["*"])

    if not isinstance(headers, dict):
        return JSONResponse({"ok": False, "error": "headers must be a dict"}, status_code=400)
    if not isinstance(enabled_tools, list):
        return JSONResponse({"ok": False, "error": "enabled_tools must be a list"}, status_code=400)

    config_path = get_config_path()
    config = load_config(config_path)

    server_config = MCPServerConfig(
        type=mcp_type,
        url=url,
        headers=headers,
        tool_timeout=tool_timeout,
        enabled_tools=enabled_tools,
    )
    config.tools.mcp_servers[name] = server_config
    config.tools.enable_mcp = True

    save_config(config, config_path)
    # Update runtime config so dashboard reflects change immediately
    request.app.state.config = config
    hot_applied = False
    restart_required = True
    agent = getattr(request.app.state, "agent", None)
    if agent is not None and hasattr(agent, "load_mcp_server"):
        try:
            hot_applied = bool(await agent.load_mcp_server(name, server_config))
            restart_required = not hot_applied
        except Exception as exc:
            logger.warning(
                "[Fleet] MCP runtime hot-load failed for key '{}': {}",
                name,
                type(exc).__name__,
            )
    elif agent is not None and hasattr(agent, "_mcp_servers"):
        try:
            agent._mcp_servers = config.tools.mcp_servers
        except Exception as exc:
            logger.warning("[Fleet] Could not update runtime MCP server map: {}", type(exc).__name__)

    logger.info(
        "[Fleet] MCP config pushed: {} -> {} (type={}, headers={})",
        name, url, mcp_type, list(headers.keys()),
    )
    return JSONResponse({
        "ok": True,
        "name": name,
        "restart_required": restart_required,
        "hot_applied": hot_applied,
    })


@router.post("/api/fleet/remove-mcp")
async def fleet_remove_mcp(request: Request):
    """Remove the Fleet MCP server entry from this bot's config.json.

    Best-effort cleanup called by Fleet Manager when releasing a bot.
    If the entry does not exist the call is still considered successful.

    Auth: Basic Auth (same as /api/health).
    Body JSON: { "name": "fleet" }  (optional, default "fleet")
    Response: { "ok": true, "name": "<name>", "removed": true|false }
    """
    from nanobot.config.loader import get_config_path, load_config, save_config

    auth_err = _verify_fleet_auth(request)
    if auth_err:
        return auth_err

    try:
        raw_body = await request.body()
        body = await request.json() if raw_body.strip() else {}
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    try:
        name = _fleet_mcp_name(body.get("name"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    config_path = get_config_path()
    config = load_config(config_path)

    removed = name in config.tools.mcp_servers
    if removed:
        del config.tools.mcp_servers[name]
        logger.info("[Fleet] MCP config removed (key '{}')", name)
    else:
        logger.debug("[Fleet] remove-mcp: key '{}' not found, nothing to remove", name)

    config.tools.enable_mcp = len(config.tools.mcp_servers) > 0
    save_config(config, config_path)
    request.app.state.config = config
    agent = getattr(request.app.state, "agent", None)
    if removed and agent is not None and hasattr(agent, "unload_mcp_server"):
        try:
            await agent.unload_mcp_server(name)
        except Exception as exc:
            logger.warning(
                "[Fleet] MCP runtime unload failed for key '{}': {}",
                name,
                type(exc).__name__,
            )
    if agent is not None and hasattr(agent, "_mcp_servers"):
        try:
            agent._mcp_servers = config.tools.mcp_servers
        except Exception as exc:
            logger.warning("[Fleet] Could not update runtime MCP server map: {}", type(exc).__name__)
    return JSONResponse({"ok": True, "name": name, "removed": removed})


@router.post("/api/fleet/restart")
async def fleet_restart(request: Request):
    """Restart the gateway process via Fleet Basic Auth.

    Companion to /config/restart which requires a browser session.
    This endpoint uses the same Basic Auth as all other /api/fleet/*
    endpoints so Fleet Manager can trigger a restart programmatically
    after pushing MCP config or making other remote config changes.

    Auth: Basic Auth (same as /api/health).
    Response: { "ok": true, "message": "Gateway is restarting..." }
    """
    import asyncio
    import os
    import sys

    auth_err = _verify_fleet_auth(request)
    if auth_err:
        return auth_err

    logger.info("[Fleet] Gateway restart requested via /api/fleet/restart")

    async def _do_restart():
        await asyncio.sleep(0.5)  # Let the response reach the client
        logger.info("[Fleet] Restarting via os.execv...")
        os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

    asyncio.create_task(_do_restart())
    return JSONResponse({"ok": True, "message": "Gateway is restarting..."})
