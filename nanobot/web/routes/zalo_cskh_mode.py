"""Dashboard opt-in with explicit saved/runtime state and a bounded undo record."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from nanobot.config.loader import load_config, save_config
from nanobot.config.paths import get_data_dir
from nanobot.config.public_mode import (
    _field,
    customer_service_enable_plan,
    customer_service_policy_key,
    policy_value,
)
from nanobot.config.schema import Config
from nanobot.web.routes.memory_graph import _require_csrf
from nanobot.web.routes.zalo_setup import _get_zalo_channel

router = APIRouter(prefix="/zalo/cskh-mode/api")
_mode_lock = RLock()
_TEMPLATES = Path(__file__).resolve().parents[2] / "templates"


def _normal(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _persona(config):
    path = config.workspace_path / "SOUL.md"
    if not path.exists():
        return "install"
    text = _normal(path.read_text(encoding="utf-8"))
    if text == _normal((_TEMPLATES / "SOUL_customer_service.md").read_text(encoding="utf-8")):
        return "already"
    if text == _normal((_TEMPLATES / "SOUL.md").read_text(encoding="utf-8")):
        return "install"
    return "keep"


def _saved(config):
    service = _field(getattr(config.channels, "zalo", {}), "customer_service", "customerService", {})
    return bool(config.public_customer_mode and _field(service, "enabled", "enabled", False))


def _target(config):
    service = _field(getattr(config.channels, "zalo", {}), "customer_service", "customerService", {})
    return _field(service, "escalation_target", "escalationTarget", "")


def _undo_blocked(request, config):
    channel = _get_zalo_channel(request)
    try:
        if channel is not None and getattr(channel, "_customer_service_enabled", False):
            state = channel._get_customer_state().snapshot()
        else:
            path = config.workspace_path / "zalo_cskh/state.json"
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"customers": {}}
        if not isinstance(state, dict) or not isinstance(state.get("customers"), dict):
            return "cskh.mode.undo_state_error"
        if state["customers"]:
            return "cskh.mode.undo_customers"
    except (OSError, ValueError, TypeError, AttributeError):
        return "cskh.mode.undo_state_error"
    return ""


def _status(request, config):
    saved = _saved(config)
    running = bool(request.app.state.public_customer_mode)
    blocked = _undo_blocked(request, config)
    undo_changes = []
    record_path = get_data_dir() / "cskh_mode_undo.json"
    if saved and record_path.exists():
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            if record["version"] != 1 or record["workspace"] != str(config.workspace_path.resolve()):
                raise ValueError("Workspace mismatch")
            data = config.model_dump(mode="json", by_alias=True)
            for change in record["changes"]:
                key = change["key"]
                if isinstance(key, str) and customer_service_policy_key(key):
                    undo_changes.append({"key": key, "old": policy_value(data, key), "new": change["old"]})
        except (OSError, ValueError, KeyError, TypeError):
            blocked = blocked or "cskh.mode.undo_record_error"
    return {"saved": saved, "running": running, "restart_needed": saved != running,
            "escalation_target": _target(config), "can_undo": saved and not blocked,
            "undo_blocked": blocked, "undo_changes": undo_changes}


def _plan(config, chat_id):
    return customer_service_enable_plan(config, chat_id,
        session_count=sum(1 for p in (config.workspace_path / "sessions").glob("*.jsonl") if p.is_file()),
        persona=_persona(config))


def _write_record(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _messages(request, result):
    # Return translated text, not raw keys, even for clients outside this page.
    from nanobot.web.i18n import resolve_locale, translate
    def tr(request, key):
        return translate(resolve_locale(request), key)
    for key in ("blockers", "warnings"):
        if key in result:
            result[key] = [tr(request, item) if item.startswith("cskh.") else item for item in result[key]]
    if result.get("undo_blocked"):
        result["undo_blocked"] = tr(request, result["undo_blocked"])
    return result


class ModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    telegram_chat_id: str = Field(default="", max_length=64)
    acknowledged: StrictBool = False


@router.get("/status")
async def status(request: Request):
    return _messages(request, _status(request, load_config()))


@router.post("/preview", dependencies=[Depends(_require_csrf)])
async def preview(request: Request, body: ModeRequest):
    with _mode_lock:
        return _messages(request, _plan(load_config(), body.telegram_chat_id).public_dict())


@router.post("/enable", dependencies=[Depends(_require_csrf)])
async def enable(request: Request, body: ModeRequest):
    if not body.acknowledged:
        raise HTTPException(400, "Acknowledgement is required")
    with _mode_lock:
        current = load_config()
        plan = _plan(current, body.telegram_chat_id)
        result = _messages(request, plan.public_dict())
        if plan.blockers:
            from fastapi.responses import JSONResponse
            return JSONResponse(result, status_code=409)
        record_path = get_data_dir() / "cskh_mode_undo.json"
        old_record = record_path.read_bytes() if record_path.exists() else None
        # Repeated clicks must not overwrite the original pre-enable settings.
        if not _saved(current):
            _write_record(record_path, {"version": 1,
                "at": datetime.now(timezone.utc).isoformat(),
                "workspace": str(current.workspace_path.resolve()), "changes": plan.changes})
        try:
            save_config(plan.config)
        except Exception:
            if old_record is None:
                record_path.unlink(missing_ok=True)
            else:
                _write_record(record_path, json.loads(old_record))
            raise
        request.app.state.config = plan.config
        if plan.persona == "install":
            soul = current.workspace_path / "SOUL.md"
            soul.parent.mkdir(parents=True, exist_ok=True)
            # Recheck before replacing: an admin may have edited since preview.
            if _persona(current) == "install":
                soul.write_bytes((_TEMPLATES / "SOUL_customer_service.md").read_bytes())
            else:
                result["persona"] = _persona(current)
        result.update(_messages(request, _status(request, plan.config)))
        return result


@router.post("/undo", dependencies=[Depends(_require_csrf)])
async def undo(request: Request, body: ModeRequest):
    if not body.acknowledged:
        raise HTTPException(400, "Acknowledgement is required")
    with _mode_lock:
        current = load_config()
        if reason := _undo_blocked(request, current):
            raise HTTPException(409, _messages(request, {"undo_blocked": reason})["undo_blocked"])
        data = current.model_dump(mode="json", by_alias=True)
        path = get_data_dir() / "cskh_mode_undo.json"
        warnings = []
        if path.exists():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if record["version"] != 1 or record["workspace"] != str(current.workspace_path.resolve()):
                    raise ValueError("Undo record belongs to another workspace")
                for change in record["changes"]:
                    key = change["key"]
                    if isinstance(key, str) and customer_service_policy_key(key):
                        # Never recreate deleted MCP servers with incomplete config.
                        if key.startswith("tools.mcpServers.") and key[len("tools.mcpServers."):-len(".enabled")] not in data["tools"]["mcpServers"]:
                            continue
                        policy_value(data, key, change["old"])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise HTTPException(409, "Undo record cannot be read safely") from exc
        else:
            warnings.append("cskh.mode.undo_no_record")
        policy_value(data, "publicCustomerMode", False)
        policy_value(data, "channels.zalo.customerService.enabled", False)
        updated = Config.model_validate(data)
        save_config(updated)
        request.app.state.config = updated
        persona = _persona(current)
        if persona == "already":
            (current.workspace_path / "SOUL.md").write_bytes((_TEMPLATES / "SOUL.md").read_bytes())
        elif persona == "keep":
            warnings.append("cskh.mode.warning_soul")
        path.unlink(missing_ok=True)
        return _messages(request, {**_status(request, updated), "warnings": warnings, "persona": persona})
