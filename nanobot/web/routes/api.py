"""API routes for real-time data endpoints."""

import asyncio
import json
import os
import re
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from loguru import logger
from starlette.responses import StreamingResponse

from nanobot.fleet.journal import Binding
from nanobot.fleet.signing import HEADER_FLEET_ID, PROTOCOL_VERSION
from nanobot.fleet.signing import verify as verify_fleet_signature

router = APIRouter()

_start_time = time.time()

# ── Fleet Claim State (runtime, not persisted) ─────────────────
_fleet_claim: dict = {
    "fleet_id": "",       # Which fleet claimed this bot
    "claimed_by": "",     # IP of the fleet-manager that claimed
    "claimed_at": "",     # ISO timestamp of claim
}

_FLEET_MCP_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]{1,64}$")
_FLEET_BOT_ID_RE = re.compile(r"[\x21-\x7e]{1,256}")  # the fleet's id for this bot, sent as an X-Bot-ID value


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
    """Lightweight liveness probe for browser restart progress pages. boot is new on every process
    start (an in-place restart keeps the PID): the restart page waits for a different one (custom)."""
    from nanobot.web.code_version import BOOT_ID

    return JSONResponse({"ok": True, "boot": BOOT_ID})


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

        binding = _fleet_binding(request)
        payload = {
            "status": "ok",
            "bot_id": bot_id,
            # The durable binding is a claim that survived a restart (custom, plan 3.3.1)
            "fleet_id": _fleet_claim["fleet_id"] or (binding.fleet_id if binding else "") or gateway.fleet_id or "",
            "fleet_claimed_by": _fleet_claim["claimed_by"],
            "fleet_claimed_at": _fleet_claim["claimed_at"],
            "model": model_label,
            "uptime": uptime_seconds,
            "channels": channels,
            "web_port": gateway.web.port,
        }
        # Fleet protocol v1 (custom, plan 3.3.6): what this code can do, and the state of the channel now.
        # Only with fleet turns over the bus; the older direct call has no journal or outbox
        turns = _fleet_turns(request)
        if turns is not None and getattr(gateway, "fleet_via_bus", True):
            payload["fleet_protocol_version"] = PROTOCOL_VERSION
            payload["fleet_capabilities"] = ["inbox", "ack"]
            payload["fleet_channel"] = turns.channel_status()
        return JSONResponse(payload)
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


# ── Fleet protocol v1: the channel binding and signed requests (custom, plan 3.3.1, 3.3.13) ──


def _fleet_turns(request: Request):
    """The agent's FleetTurns (journal, binding, outbox), or None without an agent or on the old path."""
    return getattr(getattr(request.app.state, "agent", None), "fleet_turns", None)


def _fleet_binding(request: Request) -> Binding | None:
    turns = _fleet_turns(request)
    if turns is None:
        return None
    try:
        return turns.journal.binding()
    except Exception as e:  # a journal that cannot be read must not take the fleet API down
        logger.warning("[Fleet] Could not read the channel binding: {}", e)
        return None


async def _verify_fleet_request(request: Request) -> JSONResponse | None:
    """Basic Auth as always; once bound to a fleet, every POST must also be signed with its token.

    Without a binding the X-Fleet-* headers are ignored: the first claim rests on the password alone,
    as does a bot that `nanobot fleet reset-binding` freed. The signed path is the route's own path
    (/api/fleet/claim, ...), never the URL as served, so a proxy prefix does not matter. The body is
    hashed as received. Returns the error response, or None when the request may proceed.
    """
    auth_err = _verify_fleet_auth(request)
    if auth_err:
        return auth_err
    return await _verify_fleet_signature(request)


async def _verify_fleet_signature(request: Request) -> JSONResponse | None:
    """The signature half of _verify_fleet_request, for a route that checks Basic Auth itself."""
    binding = _fleet_binding(request)
    if binding is None:
        return None
    fleet_header = request.headers.get(HEADER_FLEET_ID)
    if fleet_header and fleet_header != binding.fleet_id:
        return JSONResponse({"ok": False, "error": "fleet_mismatch"}, status_code=403)
    route = request.scope.get("route")
    canonical_path = getattr(route, "path", None) or request.url.path
    body = await request.body()
    ok, error, extra = verify_fleet_signature(binding.token, binding.token_id, request.method, canonical_path, body,
                                              request.headers, protocol_version=binding.protocol_version)
    if not ok:
        logger.warning("[Fleet] {} {} refused: {} (bound to fleet '{}', token generation {})",
                       request.method, canonical_path, error, binding.fleet_id, binding.token_id)
        return JSONResponse({"ok": False, "error": error, **extra}, status_code=401)
    return None


def _attach_channel(request: Request, body: dict, fleet_id: str) -> dict:
    """Take the channel a claim brings (inbox URL and token) into the durable binding.

    A claim without channel changes nothing. The first channel attaches; a later one with a new token
    rotates it (that claim was verified with the old token). Returns the channel block of the answer.
    channel.bot_id is the fleet's id for this bot, sent back as X-Bot-ID on every event.

    A channel this bot cannot use leaves it unattached, with the reason in the answer, but the claim
    itself still stands: a bad channel must not take the bot out of its fleet.
    """
    turns = _fleet_turns(request)
    if turns is None:
        return {"attached": False}
    channel = body.get("channel")
    if isinstance(channel, dict) and (channel.get("inbox_url") or channel.get("token")):
        inbox_url = str(channel.get("inbox_url") or "").strip()
        token = str(channel.get("token") or "")
        bot_id = str(channel.get("bot_id") or "")
        problem = ""
        try:
            token_id = int(channel.get("token_id", 0))
            protocol_version = int(channel.get("protocol_version") or PROTOCOL_VERSION)
        except (TypeError, ValueError):
            problem = "token_id and protocol_version must be integers"
        if not problem and (not inbox_url.startswith(("http://", "https://")) or not token):
            problem = "inbox_url (http or https) and token are required"
        if not problem and bot_id and not _FLEET_BOT_ID_RE.fullmatch(bot_id):
            problem = "bot_id is sent as a header value: printable ASCII without spaces, at most 256"
        if problem:
            logger.warning("[Fleet] Claim of fleet '{}' accepted without attaching its channel: {}", fleet_id, problem)
            current = turns.journal.binding()
            return {"attached": current is not None, "token_id": current.token_id if current else None,
                    "error": f"Invalid channel: {problem}"}
        new = Binding(fleet_id, inbox_url, token, token_id, protocol_version, bot_id)
        current = turns.journal.binding()
        if current != new:
            turns.journal.bind(new)
            turns.outbox.bound()
            logger.info("[Fleet] Channel {} fleet '{}': inbox {}, token generation {}",
                        "rotated for" if current is not None else "attached to", fleet_id, inbox_url, token_id)
        return {"attached": True, "token_id": token_id}
    binding = turns.journal.binding()
    return {"attached": binding is not None, "token_id": binding.token_id if binding else None}


def _bound_fleet_id(request: Request, gateway) -> str:
    """The fleet this bot answers to right now: the live claim, else the durable binding, else config."""
    binding = _fleet_binding(request)
    return _fleet_claim["fleet_id"] or (binding.fleet_id if binding else "") or getattr(gateway, "fleet_id", "") or ""


@router.post("/api/fleet/claim")
async def fleet_claim(request: Request):
    """Fleet-manager claims this bot. Stores fleet_id + caller IP in runtime.

    - If unclaimed: accept, store fleet_id + IP
    - If same fleet_id: idempotent, update IP/timestamp
    - If different fleet_id: reject (409 Conflict)

    Fleet protocol v1 (custom, plan 3.3.1): the body may carry ``channel`` {inbox_url, token, token_id,
    protocol_version}; it is kept in the durable binding, which then counts as the claim across restarts
    and makes every later fleet POST require that token's signature.
    """
    global _fleet_claim

    auth_err = await _verify_fleet_request(request)
    if auth_err:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
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

    # The durable binding is a claim that survived a restart: another fleet needs a release first
    binding = _fleet_binding(request)
    if binding is not None and binding.fleet_id != fleet_id:
        return JSONResponse({
            "ok": False,
            "error": f"Bot is bound to fleet '{binding.fleet_id}'. Release it first.",
            "claimed_by_fleet": binding.fleet_id,
        }, status_code=409)

    if current and current != fleet_id:
        return JSONResponse({
            "ok": False,
            "error": f"Bot already claimed by fleet '{current}' (from {_fleet_claim['claimed_by']}). Release first.",
            "claimed_by_fleet": current,
            "claimed_by_ip": _fleet_claim["claimed_by"],
            "claimed_at": _fleet_claim["claimed_at"],
        }, status_code=409)

    channel_state = _attach_channel(request, body, fleet_id)

    # Accept claim (or update existing)
    _fleet_claim = {
        "fleet_id": fleet_id,
        "claimed_by": client_ip,
        "claimed_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("[Fleet] Bot claimed by fleet '{}' from {}", fleet_id, client_ip)
    return JSONResponse({"ok": True, "fleet_id": fleet_id, "claimed_by": client_ip, "channel": channel_state})


@router.post("/api/fleet/release")
async def fleet_release(request: Request):
    """Release this bot from its fleet. Only the claiming fleet can release.

    Fleet protocol v1 (custom, plan 3.3.1): ``revoke: true`` also drops the durable channel binding and
    the events still waiting for that fleet; a bound bot only accepts a release signed with its token.
    """
    global _fleet_claim

    auth_err = await _verify_fleet_request(request)
    if auth_err:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    fleet_id = (body.get("fleet_id", "") or "").strip()
    if not fleet_id:
        return JSONResponse({"ok": False, "error": "fleet_id is required"}, status_code=400)

    # Check the runtime claim, the durable binding AND config fleet_id
    current_runtime = _fleet_claim["fleet_id"]
    binding = _fleet_binding(request)
    config_fleet = getattr(request.app.state.config.gateway, "fleet_id", "")
    current = current_runtime or (binding.fleet_id if binding else "") or config_fleet

    if not current:
        return JSONResponse({"ok": True, "message": "Bot is not claimed by any fleet"})

    if current != fleet_id:
        return JSONResponse({
            "ok": False,
            "error": f"Bot is claimed by fleet '{current}', not '{fleet_id}'. Only '{current}' can release.",
        }, status_code=403)

    revoked = False
    turns = _fleet_turns(request)
    if body.get("revoke") and turns is not None and binding is not None:
        if turns.journal.unbind() is not None:
            revoked = True
            turns.outbox.bound()  # nothing left to pause for
            logger.info("[Fleet] Channel binding to fleet '{}' revoked; its pending events dropped", fleet_id)

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
    return JSONResponse({"ok": True, "revoked": revoked})


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


_FLEET_TURN_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


async def _fleet_turn_over_bus(turns, body: dict, from_bot: str, content: str, session_key: str,
                               fleet_id: str = "") -> JSONResponse:
    """Run a fleet message as a bus turn and answer from its terminal (custom, nanobot/fleet/turns.py).
    fleet_id: the fleet asking; its events go only to that fleet's inbox (plan 3.3.1)."""
    import uuid

    from nanobot.fleet.turns import FleetTurnResult, TurnConflict

    mode = body.get("mode") or "sync"
    if mode not in ("sync", "ack"):
        return JSONResponse({"ok": False, "error": f"mode '{mode}' is not supported"}, status_code=400)
    turn_id = body.get("turn_id")
    if turn_id is None:
        turn_id = f"local-{uuid.uuid4().hex}"  # Fleet Manager before protocol v1 sends none
    elif not isinstance(turn_id, str) or not _FLEET_TURN_ID_RE.match(turn_id):
        return JSONResponse({"ok": False, "error": "Invalid turn_id"}, status_code=400)
    timeout = body.get("timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        timeout = None
    principal = body.get("principal") if isinstance(body.get("principal"), dict) else None

    try:
        result = await turns.submit(content=content, from_bot=from_bot, session_key=session_key, mode=mode,
                                    turn_id=turn_id, principal=principal, timeout=timeout, fleet_id=fleet_id)
    except TurnConflict:
        return JSONResponse({"ok": False, "error": "turn_id_conflict", "turn_id": turn_id}, status_code=409)
    except asyncio.TimeoutError:
        return JSONResponse(
            {"ok": False, "error": "Turn still running after timeout_seconds", "turn_id": turn_id},
            status_code=504,
        )
    except asyncio.CancelledError:
        logger.warning("[Fleet] Message request cancelled while turn {} was running; returning controlled 499",
                       turn_id)
        return JSONResponse({"ok": False, "error": "Fleet message request cancelled", "turn_id": turn_id},
                            status_code=499)

    if mode == "ack" and not FleetTurnResult.is_terminal(result.state):
        # The turn runs on; everything it sends reaches Fleet Manager's inbox (plan 3.3.5, P2)
        logger.info("[Fleet] Accepted turn {} from '{}' ({})", turn_id, from_bot, result.state)
        return JSONResponse({"ok": True, "turn_id": turn_id, "state": result.state}, status_code=202)
    if result.state == "completed":
        logger.info("[Fleet] Reply to '{}': {}", from_bot, result.response[:100] if result.response else "(empty)")
        return JSONResponse({"ok": True, "turn_id": turn_id, "state": "completed", "response": result.response})
    if result.state == "cancelled":
        return JSONResponse({"ok": False, "error": "Turn cancelled", "turn_id": turn_id}, status_code=499)
    return JSONResponse({"ok": False, "error": result.error or "Turn failed", "turn_id": turn_id}, status_code=500)


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
        # Bound to a fleet: the request must be signed with its token too (custom, plan 3.3.13)
        if (signature_err := await _verify_fleet_signature(request)) is not None:
            return signature_err

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

        # A fleet message runs as a bus turn and answers from its terminal (custom, nanobot/fleet);
        # gateway.fleetViaBus = false keeps the direct call below until the move is proven (plan P5)
        turns = getattr(agent, "fleet_turns", None)
        if turns is not None and getattr(config.gateway, "fleet_via_bus", True):
            return await _fleet_turn_over_bus(turns, body, from_bot, content, session_key,
                                              fleet_id=_bound_fleet_id(request, config.gateway))

        # One turn at a time per session, the same lock Web Chat and the bus path take
        # (AgentLoop._dispatch): without it, Fleet Manager timing out and sending the next
        # message ran two turns over one session history. The lock is taken inside the
        # shielded coroutine so a request Fleet Manager has already given up on keeps
        # holding it until its turn really ends.
        session_locks = getattr(agent, "_session_locks", None)
        session_lock = (
            session_locks.setdefault(session_key, asyncio.Lock())
            if session_locks is not None else nullcontext()
        )

        async def _run_turn():
            async with session_lock:
                return await agent.process_direct(
                    content=content,
                    session_key=session_key,
                    channel="fleet",
                    chat_id=from_bot,
                )

        try:
            resp = await asyncio.shield(_run_turn())
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

    auth_err = await _verify_fleet_request(request)
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

    auth_err = await _verify_fleet_request(request)
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

    auth_err = await _verify_fleet_request(request)
    if auth_err:
        return auth_err

    logger.info("[Fleet] Gateway restart requested via /api/fleet/restart")

    async def _do_restart():
        await asyncio.sleep(0.5)  # Let the response reach the client
        logger.info("[Fleet] Restarting via os.execv...")
        os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

    asyncio.create_task(_do_restart())
    return JSONResponse({"ok": True, "message": "Gateway is restarting..."})
