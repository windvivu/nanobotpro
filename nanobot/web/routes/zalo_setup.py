"""Zalo Setup — Web routes for managing Zalo connection."""

import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from loguru import logger

router = APIRouter()


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
        }
    except Exception:
        zalo_config = {
            "enabled": False,
            "allow_from": "*",
            "node_path": "node",
            "group_reply_mode": "ambient",
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

    return request.app.state.templates.TemplateResponse(request, "zalo_setup.html", {
        "zalo_status": status_data,
        "zalo_config": zalo_config,
        "bridge_status": bridge_status,
    })


@router.post("/zalo/connect")
async def zalo_connect(request: Request):
    """Trigger Zalo QR login."""
    zalo_channel = _get_zalo_channel(request)
    if not zalo_channel:
        return JSONResponse({"status": "error", "message": "Zalo channel not enabled"}, status_code=400)

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

    if zalo_channel._status != zalo_channel.STATUS_DISCONNECTED:
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
    from nanobot.config.loader import load_config, save_config
    from nanobot.channels.zalo_personal import ZaloPersonalConfig
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON"}, status_code=400)

    try:
        cfg = load_config()

        # allow_from: space/comma-separated string → list
        allow_from_raw = body.get("allow_from", "*").strip()
        if not allow_from_raw:
            allow_from_raw = "*"
        allow_from_list = [x.strip() for x in allow_from_raw.replace(",", " ").split() if x.strip()]

        # node_path: simple string
        node_path = body.get("node_path", "node").strip() or "node"
        group_reply_mode = body.get("group_reply_mode") or body.get("groupReplyMode") or "ambient"
        if group_reply_mode not in {"mention", "ambient", "open"}:
            return JSONResponse(
                {"success": False, "error": "Invalid groupReplyMode"},
                status_code=400,
            )

        # Support both dict (extra field) and typed object for channel config
        zalo_raw = getattr(cfg.channels, "zalo", None)
        if zalo_raw is None:
            zalo_raw = {}
            setattr(cfg.channels, "zalo", zalo_raw)

        if isinstance(zalo_raw, dict):
            zalo_raw["allow_from"] = allow_from_list
            zalo_raw["node_path"] = node_path
            zalo_raw["groupReplyMode"] = group_reply_mode
        elif zalo_raw is not None:
            zalo_raw.allow_from = allow_from_list
            zalo_raw.node_path = node_path
            zalo_raw.group_reply_mode = group_reply_mode

        save_config(cfg)

        # Apply live to the running channel so the next Zalo event uses the new mode.
        zalo_channel = _get_zalo_channel(request)
        if zalo_channel:
            live_data = {
                "enabled": True,
                "allowFrom": allow_from_list,
                "nodePath": node_path,
                "groupReplyMode": group_reply_mode,
            }
            current_enabled = getattr(zalo_channel.config, "enabled", True)
            live_data["enabled"] = current_enabled
            zalo_channel.config = ZaloPersonalConfig.model_validate(live_data)
            zalo_channel._node_path = node_path

        logger.info(
            "Zalo config saved: allow_from={}, node_path={}, group_reply_mode={}",
            allow_from_list,
            node_path,
            group_reply_mode,
        )
        return JSONResponse({"success": True, "message": "Saved. Applies to the next Zalo message."})
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

    # Must be disconnected first
    if zalo_channel and zalo_channel._status != zalo_channel.STATUS_DISCONNECTED:
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
