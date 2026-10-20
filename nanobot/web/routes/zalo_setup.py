"""Zalo Setup — Web routes for managing Zalo connection."""

import asyncio
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from loguru import logger

from nanobot.config.loader import ConfigSaveError

router = APIRouter()
# Connects started in the background while the bridge installs (kept referenced).
_connect_tasks: set[asyncio.Task] = set()


def _get_zalo_channel(request: Request):
    """Get the ZaloPersonalChannel from app state."""
    channel_manager = getattr(request.app.state, "channel_manager", None)
    if channel_manager:
        return channel_manager.get_channel("zalo")
    return None


def _zget(obj, key, default=None):
    """Read Zalo config values from either dict extras or typed config objects."""
    if isinstance(obj, dict):
        if key in obj:
            return obj.get(key, default)
        camel = "".join([key.split("_")[0], *[part.capitalize() for part in key.split("_")[1:]]])
        return obj.get(camel, default)
    return getattr(obj, key, default)


@router.get("/zalo/setup", response_class=HTMLResponse)
async def zalo_setup_page(request: Request):
    """Render the Zalo setup page."""
    from nanobot.config.loader import load_config
    zalo_channel = _get_zalo_channel(request)

    status_data = {"status": "not_enabled", "hasQr": False, "userId": "", "userName": ""}
    if zalo_channel:
        status_data = zalo_channel.get_status()

    # Load current Zalo config fields for the config card
    try:
        cfg = load_config()
        zalo_raw = getattr(cfg.channels, "zalo", None) or {}
        # Support both dict (extra field) and typed object
        zalo_config = {
            "enabled": _zget(zalo_raw, "enabled", False),
            "allow_from": " ".join(_zget(zalo_raw, "allow_from", ["*"])) or "*",
            "node_path": _zget(zalo_raw, "node_path", "node"),
            "group_reply_mode": _zget(zalo_raw, "group_reply_mode", "ambient"),
            "group_filter": _zget(zalo_raw, "group_filter", "log"),
        }
    except Exception:
        zalo_config = {
            "enabled": False,
            "allow_from": "*",
            "node_path": "node",
            "group_reply_mode": "ambient",
            "group_filter": "log",
        }

    # Check bridge installation status (persists across page refreshes)
    try:
        from nanobot.config.paths import get_data_dir
        bridge_dir = get_data_dir() / "zalo_bridge"
        node_modules = bridge_dir / "node_modules"
        zca_pkg = node_modules / "zca-js" / "package.json"
        if node_modules.exists() and zca_pkg.exists():
            import json as _json
            zca_ver = _json.loads(zca_pkg.read_text()).get("version", "?")
            bridge_status = {"installed": True, "version": zca_ver}
        else:
            bridge_status = {"installed": False, "version": None}
    except Exception:
        bridge_status = {"installed": None, "version": None}  # unknown

    from nanobot.web.auth import get_csrf_cookie_name
    from nanobot.web.routes.dashboard import active_profile

    profile_id, profile_name = active_profile(request.app.state.config.workspace_path)
    return request.app.state.templates.TemplateResponse(request, "zalo_setup.html", {
        "profile_id": profile_id,
        "profile_name": profile_name,
        "zalo_status": status_data,
        "zalo_config": zalo_config,
        "bridge_status": bridge_status,
        "csrf_cookie": get_csrf_cookie_name(),
    })


@router.post("/zalo/connect")
async def zalo_connect(request: Request):
    """Trigger Zalo QR login."""
    zalo_channel = _get_zalo_channel(request)
    if not zalo_channel:
        return JSONResponse({"status": "error", "message": "Zalo channel not enabled"}, status_code=400)

    if not zalo_channel._bridge_installed():
        # The first Connect installs the bridge (npm install, can take minutes).
        # Answer at once and let the page follow it through /zalo/status, instead
        # of holding the request open with nothing to show.
        task = asyncio.create_task(zalo_channel.connect())
        _connect_tasks.add(task)
        task.add_done_callback(_connect_tasks.discard)

        def surface_error(done: asyncio.Task) -> None:
            result = None if done.cancelled() or done.exception() else done.result()
            if isinstance(result, dict) and result.get("status") == "error":
                zalo_channel._setup_state = "failed"
                zalo_channel._setup_error = str(result.get("message", "connect failed"))[:600]

        task.add_done_callback(surface_error)
        await asyncio.sleep(0)  # let connect() mark itself as connecting
        return JSONResponse({**zalo_channel.get_status(), "status": "installing"})

    result = await zalo_channel.connect()
    return JSONResponse(result)


@router.post("/zalo/disconnect")
async def zalo_disconnect(request: Request):
    """Disconnect from Zalo."""
    zalo_channel = _get_zalo_channel(request)
    if not zalo_channel:
        return JSONResponse({"status": "error", "message": "Zalo channel not enabled"}, status_code=400)

    result = await zalo_channel.disconnect()
    # Merge with get_status() so UI always has hasSavedSession, hasQr, etc.
    status = zalo_channel.get_status()
    return JSONResponse({**status, **result})


@router.post("/zalo/clear-session")
async def zalo_clear_session(request: Request):
    """Delete saved credentials to force QR re-login (e.g. switch accounts)."""
    zalo_channel = _get_zalo_channel(request)
    if not zalo_channel:
        return JSONResponse({"status": "error", "message": "Zalo channel not enabled"}, status_code=400)

    # AUTH_REQUIRED counts as disconnected: the bridge exited after the session expired.
    if zalo_channel._status not in (zalo_channel.STATUS_DISCONNECTED, zalo_channel.STATUS_AUTH_REQUIRED):
        return JSONResponse({"status": "error", "message": "Disconnect first before clearing session"}, status_code=400)

    cleared = False
    if zalo_channel._bridge_dir:
        creds_path = zalo_channel._bridge_dir / "credentials.json"
        if creds_path.exists():
            creds_path.unlink()
            cleared = True
            logger.info("Zalo saved credentials cleared by user")

    status = zalo_channel.get_status()
    return JSONResponse({**status, "cleared": cleared})


@router.get("/zalo/status")
async def zalo_status(request: Request):
    """Get current Zalo connection status."""
    zalo_channel = _get_zalo_channel(request)
    if not zalo_channel:
        return JSONResponse({"status": "not_enabled"})

    return JSONResponse(zalo_channel.get_status())


@router.get("/zalo/bridge-info")
async def zalo_bridge_info(request: Request):
    """Return bridge installation status (zca-js version)."""
    try:
        from nanobot.config.paths import get_data_dir
        import json as _json
        node_modules = get_data_dir() / "zalo_bridge" / "node_modules"
        zca_pkg = node_modules / "zca-js" / "package.json"
        if node_modules.exists() and zca_pkg.exists():
            ver = _json.loads(zca_pkg.read_text()).get("version", "?")
            return JSONResponse({"installed": True, "version": ver})
        return JSONResponse({"installed": False, "version": None})
    except Exception as e:
        return JSONResponse({"installed": None, "version": None, "error": str(e)})


@router.get("/zalo/qr")
async def zalo_qr_image(request: Request):
    """Serve the QR code image."""
    zalo_channel = _get_zalo_channel(request)
    if not zalo_channel:
        return JSONResponse({"error": "Zalo channel not enabled"}, status_code=400)

    qr_path = zalo_channel.get_qr_path()
    if qr_path and qr_path.exists():
        # Add cache-busting
        return FileResponse(
            str(qr_path),
            media_type="image/png",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    return JSONResponse({"error": "QR code not available"}, status_code=404)


@router.post("/zalo/config")
async def zalo_save_config(request: Request):
    """Save Zalo channel configuration."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON"}, status_code=400)
    return await save_zalo_config(request, body)


async def save_zalo_config(request: Request, body: dict):
    """Shared validated save path for setup and the CSKH settings panel."""
    from nanobot.config.loader import load_config, save_config
    from nanobot.channels.zalo_personal import (
        ZaloPersonalConfig, ZaloCustomerServiceConfig, customer_service_target_error,
    )
    from pydantic import ValidationError
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "error": "Expected JSON object"}, status_code=400)
    try:
        cfg = load_config()

        # A nested CSKH patch must preserve the existing channel settings.
        zalo_raw = getattr(cfg.channels, "zalo", None)
        if zalo_raw is None:
            zalo_raw = {}
            setattr(cfg.channels, "zalo", zalo_raw)
        current = ZaloPersonalConfig.model_validate(
            zalo_raw if isinstance(zalo_raw, dict) else zalo_raw.model_dump()
        )
        allow_from_raw = _zget(body, "allow_from", current.allow_from)
        if isinstance(allow_from_raw, str):
            allow_from_raw = allow_from_raw.strip() or "*"
            allow_from_list = allow_from_raw.replace(",", " ").split()
        elif isinstance(allow_from_raw, list) and all(isinstance(item, str) for item in allow_from_raw):
            allow_from_list = allow_from_raw
        else:
            return JSONResponse({"success": False, "error": "Invalid allowFrom"}, status_code=400)
        node_path = _zget(body, "node_path", current.node_path)
        if not isinstance(node_path, str):
            return JSONResponse({"success": False, "error": "Invalid nodePath"}, status_code=400)
        node_path = node_path.strip() or "node"
        group_reply_mode = _zget(body, "group_reply_mode", current.group_reply_mode)
        if group_reply_mode not in {"mention", "ambient", "open"}:
            return JSONResponse({"success": False, "error": "Invalid groupReplyMode"}, status_code=400)
        group_filter = _zget(body, "group_filter", current.group_filter)
        if group_filter not in {"off", "log", "on"}:
            return JSONResponse({"success": False, "error": "Invalid groupFilter"}, status_code=400)

        # Preserve CSKH settings when the existing setup page saves its old fields.
        current_service = ZaloCustomerServiceConfig.model_validate(_zget(zalo_raw, "customer_service", {}) or {})
        update = _zget(body, "customer_service", None)
        if update is not None:
            if not isinstance(update, dict):
                return JSONResponse({"success": False, "error": "customerService must be an object"}, status_code=400)
            # Normalize each supplied key before merging, supporting partial camel/snake updates.
            fields = ZaloCustomerServiceConfig.model_fields
            aliases = {field.alias: name for name, field in fields.items()}
            data = current_service.model_dump()
            data.update({aliases.get(key, key): value for key, value in update.items()})
            try:
                current_service = ZaloCustomerServiceConfig.model_validate(data)
            except ValidationError as exc:
                return JSONResponse({"success": False, "error": str(exc)}, status_code=400)
        zalo_channel = _get_zalo_channel(request)
        effective_service = current_service.model_copy(deep=True)
        if zalo_channel:
            effective_service.enabled = getattr(zalo_channel, "_customer_service_enabled", False)
        if error := customer_service_target_error(effective_service, cfg):
            return JSONResponse({"success": False, "error": error}, status_code=400)

        if isinstance(zalo_raw, dict):
            for key in ("customer_service", "allow_from", "node_path", "group_reply_mode", "group_filter"):
                zalo_raw.pop(key, None)
            zalo_raw["customerService"] = current_service.model_dump(by_alias=True)
            zalo_raw["allowFrom"] = allow_from_list
            zalo_raw["nodePath"] = node_path
            zalo_raw["groupReplyMode"] = group_reply_mode
            zalo_raw["groupFilter"] = group_filter
        elif zalo_raw is not None:
            zalo_raw.allow_from = allow_from_list
            zalo_raw.node_path = node_path
            zalo_raw.group_reply_mode = group_reply_mode
            zalo_raw.group_filter = group_filter
            zalo_raw.customer_service = current_service

        save_config(cfg)
        if app := getattr(request, "app", None):
            app.state.config = cfg

        # Apply live to the running channel so the next Zalo event uses the new mode.
        zalo_channel = _get_zalo_channel(request)
        if zalo_channel:
            live_data = {
                "enabled": True,
                "allowFrom": allow_from_list,
                "nodePath": node_path,
                "groupReplyMode": group_reply_mode,
                "groupFilter": group_filter,
                "customerService": current_service.model_dump(by_alias=True),
            }
            current_enabled = getattr(zalo_channel.config, "enabled", True)
            live_data["enabled"] = current_enabled
            zalo_channel.apply_config(ZaloPersonalConfig.model_validate(live_data))

        # The escalate tool keeps its own copy of these settings: hand it the new ones, so a new
        # Telegram target or handoff text applies without a restart (custom, review 12 #5)
        agent = getattr(app.state, "agent", None) if app is not None else None
        tools = getattr(agent, "tools", None)
        tool = tools.get("escalate") if tools is not None else None
        if tool is not None and hasattr(tool, "apply_settings"):
            tool.apply_settings(current_service)
        holder = getattr(agent, "hold_messages", None)  # hold messages: on/off, delay, phrases
        if holder is not None:
            holder.apply_settings(current_service)

        logger.info(
            "Zalo config saved: allow_from={}, node_path={}, group_reply_mode={}, group_filter={}",
            allow_from_list,
            node_path,
            group_reply_mode,
            group_filter,
        )
        return JSONResponse({"success": True, "message": "Saved. Applies to the next Zalo message."})
    except ConfigSaveError:
        raise
    except Exception as e:
        logger.error("Failed to save Zalo config: {}", e)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@router.post("/zalo/reinstall-bridge")
async def zalo_reinstall_bridge(request: Request):
    """Delete bridge node_modules to force reinstall of latest zca-js on next connect.

    Useful when zca-js has a bug or the bridge fails to start due to outdated dependencies.
    """
    import shutil

    zalo_channel = _get_zalo_channel(request)

    # Must be disconnected first (AUTH_REQUIRED too: its bridge already exited)
    if zalo_channel and zalo_channel._status not in (
        zalo_channel.STATUS_DISCONNECTED, zalo_channel.STATUS_AUTH_REQUIRED
    ):
        return JSONResponse(
            {"success": False, "error": "Disconnect from Zalo before reinstalling."},
            status_code=400,
        )

    try:
        from nanobot.config.paths import get_data_dir
        bridge_dir = get_data_dir() / "zalo_bridge"
        node_modules = bridge_dir / "node_modules"

        if node_modules.exists():
            shutil.rmtree(node_modules)
            logger.info("[Zalo] Removed bridge node_modules for reinstall")
            msg = "node_modules removed. Bridge will reinstall on next Connect."
        else:
            msg = "node_modules already clean (nothing to remove)."

        # Reset channel._bridge_dir so connect() re-runs _setup_bridge() → npm install
        # Without this, connect() skips _setup_bridge() because _bridge_dir is already set.
        if zalo_channel:
            zalo_channel._bridge_dir = None
            logger.info("[Zalo] Reset bridge_dir to force npm reinstall on next connect")

        return JSONResponse({"success": True, "message": msg})
    except Exception as e:
        logger.error("[Zalo] Failed to remove node_modules: {}", e)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
