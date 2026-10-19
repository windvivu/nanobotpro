"""Authenticated admin surface for a public Zalo customer service instance."""

import json
import re
import secrets
import time
from collections import deque
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.concurrency import run_in_threadpool

from nanobot.agent.tools.escalate import build_escalation_notice
from nanobot.brain_memory.errors import BrainMemoryError, ConflictError, DisabledError
from nanobot.brain_memory.paths import resolve_ref
from nanobot.bus.events import OutboundMessage
from nanobot.channels.zalo_cskh_state import TEST_CUSTOMER_PREFIX, is_test_customer, valid_group_id
from nanobot.channels.zalo_personal import ZaloCustomerServiceConfig, customer_service_target_error
from nanobot.security.public_output import public_error
from nanobot.session.manager import SessionManager
from nanobot.utils.helpers import install_customer_service_soul
from nanobot.web import kb_catalog
from nanobot.web.auth import get_csrf_cookie_name
from nanobot.web.routes.memory_graph import _brain_config, _public_mode, _require_csrf
from nanobot.web.routes.zalo_setup import _get_zalo_channel, _zget, save_zalo_config


def service_config(config):
    return ZaloCustomerServiceConfig.model_validate(
        _zget(getattr(config.channels, "zalo", None), "customer_service", {}) or {}
    )


def cskh_enabled(config, public_mode=None):
    try:
        return (
            bool(config.public_customer_mode if public_mode is None else public_mode)
            and service_config(config).enabled
        )
    except (ValueError, AttributeError, TypeError):
        return False


def _guard(request: Request):
    if not request.app.state.customer_service_enabled:
        raise HTTPException(404, "Customer service is not enabled")
    if request.method == "POST":
        _require_csrf(request)


router = APIRouter(prefix="/zalo/cskh", dependencies=[Depends(_guard)])


def _state(request):
    channel = _get_zalo_channel(request)
    state = channel._get_customer_state() if channel else None
    if state is None:
        raise HTTPException(503, "Zalo channel is unavailable")
    return channel, state


def _customer(request, customer_id):
    _, state = _state(request)
    customer = state.snapshot()["customers"].get(customer_id)
    if customer is None:
        raise HTTPException(404, "Customer not found")
    return state, customer


_MEDIA_MARKER = re.compile(r"\[(image|file|audio|video|voice|sticker|document)(?::[^\]]*)?\]")


def _shown(content, workspace):
    """What the staff member reads: a message stored as blocks becomes its text; a media
    marker loses the path it carries for the model (a server path is not for the page)."""
    if isinstance(content, list):
        parts = [block.get("text", "") if isinstance(block, dict) else str(block) for block in content]
        content = "\n".join(part for part in parts if part)
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    from nanobot.security.public_output import redact_workspace
    return redact_workspace(_MEDIA_MARKER.sub(lambda m: f"[{m.group(1)}]", content), workspace)


def _transcript(request, customer_id, limit):
    _customer(request, customer_id)
    key = f"zalo:{customer_id}"
    path = resolve_ref(
        request.app.state.config.workspace_path, f"sessions/{SessionManager.safe_key(key)}.jsonl"
    )
    if not path.exists():
        return []
    # Pure file reader: SessionManager.read_session_file repairs corrupt files,
    # which must not happen from this read-only endpoint. Never read legacy paths.
    rows = deque(maxlen=limit)
    try:
        with path.open(encoding="utf-8") as stream:
            header = json.loads(stream.readline(65536))
            if header.get("_type") != "metadata" or header.get("key") != key:
                raise ValueError("Session principal mismatch")
            for line in stream:
                item = json.loads(line)
                if item.get("role") == "assistant" and str(item.get("content", "")).strip() == "__SILENT__":
                    continue  # the bot chose not to answer a closing message
                if item.get("role") in {"user", "assistant"}:
                    rows.append({"role": item["role"], "timestamp": item.get("timestamp", ""),
                                 "content": _shown(item.get("content", ""), request.app.state.config.workspace_path)})
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(409, "Session cannot be read safely") from exc
    return list(rows)


def _bus(request):
    manager = getattr(request.app.state, "channel_manager", None)
    bus = getattr(manager, "bus", None)
    if bus is None:
        raise HTTPException(503, "Message bus unavailable")
    return bus


async def _telegram(request, content):
    config = request.app.state.config
    service = service_config(config)
    if error := customer_service_target_error(service, config):
        raise HTTPException(409, error)
    bus = _bus(request)
    try:
        await bus.publish_outbound(
            OutboundMessage(
                channel="telegram",
                chat_id=service.escalation_target.partition(":")[2].strip(),
                content=content,
                metadata={"_escalation": True},
            )
        )
        return True
    except Exception as exc:
        public_error(exc)
        return False


@router.get("", response_class=HTMLResponse)
async def page(request: Request):
    _state(request)
    return request.app.state.templates.TemplateResponse(
        request,
        "zalo_cskh.html",
        {
            "csrf_cookie": get_csrf_cookie_name(),
        },
    )


def _needs_attention(state, key, customer) -> bool:
    """Escalated and not yet resumed: a staff member should look at this customer."""
    return state.customer_status(key) == "paused" and any(
        entry["at"] > (customer.get("escalation_cleared_at") or 0) for entry in customer["escalations"])


NEW_GROUP_DAYS = 7  # a group not yet allowed counts on the Groups tab this long after it was first seen


@router.get("/api/overview")
async def overview(request: Request):
    channel, state = _state(request)
    snapshot = state.snapshot()
    docs = await run_in_threadpool(
        kb_catalog.refresh_fact_counts, request.app.state.config.workspace_path,
        _brain_config(request), tolerate_errors=True,
    )
    from nanobot.agent.unanswered import UnansweredLog
    unanswered = len(await run_in_threadpool(UnansweredLog(request.app.state.config.workspace_path).entries))
    learning_errors = sum(bool(doc["learning"].get("error")) or bool(doc["learning"].get("failed")) for doc in docs)
    recent = time.time() - NEW_GROUP_DAYS * 86400
    return {
        "bridge": channel.get_status(),
        "public_customer_mode": _public_mode(request),
        "escalation_target": service_config(request.app.state.config).escalation_target,
        "paused_all": snapshot["paused_all"],
        "daily": snapshot["daily"].get(date.today().isoformat(), {}),
        "paused_customers": sum(
            state.customer_status(key) == "paused" for key in snapshot["customers"]
        ),
        "learning_pending": sum(doc["learning"]["in_progress"] for doc in docs),
        "learning_errors": sum(bool(doc["learning"].get("error")) for doc in docs),
        # What waits for the admin on each tab of the page
        "badges": {
            "customers": sum(_needs_attention(state, key, c) for key, c in snapshot["customers"].items()),
            "knowledge": unanswered + learning_errors,
            "groups": sum(not g["allowed"] and g["first_seen"] >= recent for g in snapshot.get("groups", {}).values()),
        },
        "kb": {
            status: sum(doc["status"] == status for doc in docs)
            for status in ("published", "pending")
        },
    }


@router.get("/api/customers")
async def customers(
    request: Request,
    status: str = "",
    q: str = Query("", max_length=200),
    limit: int = Query(50, ge=1, le=500),
):
    _, state = _state(request)
    if status not in {"", "active", "paused", "blocked"}:
        raise HTTPException(400, "Invalid customer status")
    rows = []
    for key, customer in state.snapshot()["customers"].items():
        effective = state.customer_status(key)
        if (status and status != effective) or q.casefold() not in (
            key + " " + customer["name"]
        ).casefold():
            continue
        rows.append(
            {
                "id": key,
                "name": customer["name"],
                "last_seen": customer["last_seen"],
                "status": effective,
                "messages_in": customer["messages_in"],
                "messages_out": customer["messages_out"],
                "limited_count": customer["limited_count"],
                "escalations": len(customer["escalations"]),
                "test": is_test_customer(key),
                "attention": _needs_attention(state, key, customer),
                # When a paused customer can write to the bot again (None: until resumed).
                "paused_until": customer.get("paused_until") if effective == "paused" else None,
            }
        )
    return {"customers": sorted(rows, key=lambda row: row["last_seen"], reverse=True)[:limit]}


@router.get("/api/customers/{customer_id}/transcript")
async def transcript(request: Request, customer_id: str, limit: int = Query(50, ge=1, le=200)):
    return {"messages": _transcript(request, customer_id, limit)}


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=500)


@router.post("/api/customers/{customer_id}/escalate")
async def escalate(request: Request, customer_id: str, body: Summary):
    state, customer = _customer(request, customer_id)
    service = service_config(request.app.state.config)
    content = build_escalation_notice(
        request.app.state.config.workspace_path,
        customer_id,
        customer["name"],
        "other",
        body.summary,
        _transcript(request, customer_id, 10),
    )
    delivered = await _telegram(request, content)
    state.record_escalation(
        customer_id, "other", delivered, time.time() + service.pause_after_escalation_minutes * 60
    )
    return {"published_to_bus": delivered}


STAFF_PREFIX = "[Nhân viên] "


class StaffReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=2000)


@router.post("/api/customers/{customer_id}/reply")
async def staff_reply(request: Request, customer_id: str, body: StaffReply):
    """Answer a customer as staff: the bot's Zalo account sends it, the bot's conversation
    records it, and the bot stays quiet for this customer until Resume or the pause ends."""
    channel, state = _state(request)
    _customer(request, customer_id)  # only customers who have written to the bot
    text = body.content.strip()
    if not text:
        raise HTTPException(400, "Reply is empty")
    if not is_test_customer(customer_id) and channel.get_status().get("status") != "connected":
        raise HTTPException(409, "Zalo is not connected; the reply was not sent")
    # Hold first, so a message the customer sends while the reply is on its way is kept for
    # staff rather than answered by the bot; undo the hold if Zalo did not take the reply.
    before = state.snapshot()["customers"].get(customer_id, {})
    pause = service_config(request.app.state.config).pause_after_escalation_minutes * 60
    state.hold_for_staff(customer_id, time.time() + pause)
    try:
        # _confirm: no ack from Zalo in time, or not connected, raises; nothing is recorded then.
        await channel.send(OutboundMessage(channel="zalo", chat_id=customer_id, content=text,
                                           metadata={"thread_type": "User", "_staff_reply": True,
                                                     "_confirm": True}))
    except Exception as exc:
        if before.get("status", "active") != "blocked":
            state.set_status(customer_id, before.get("status", "active"), paused_until=before.get("paused_until"))
        public_error(exc)
        raise HTTPException(502, "Zalo did not confirm delivery; the reply was not recorded") from exc
    sessions = getattr(request.app.state, "session_manager", None)
    if sessions is not None:
        session = sessions.get_or_create(f"zalo:{customer_id}")
        session.add_message("assistant", STAFF_PREFIX + text)
        sessions.save(session)
    customer = state.snapshot()["customers"].get(customer_id, {})
    return {"sent": True, "status": state.customer_status(customer_id),
            "paused_until": customer.get("paused_until")}


@router.post("/api/customers/{customer_id}/{action}")
async def customer_action(request: Request, customer_id: str, action: str):
    state, _ = _customer(request, customer_id)
    if action not in {"resume", "block", "unblock"}:
        raise HTTPException(404, "Unknown customer action")
    if action == "resume":
        state.resume_customer(customer_id)
    else:
        state.set_status(customer_id, "blocked" if action == "block" else "active")
    return {"status": state.customer_status(customer_id)}


_TEST_ID = re.compile(re.escape(TEST_CUSTOMER_PREFIX) + r"[0-9a-f]{8}")


class TestMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    content: str = Field(min_length=1, max_length=2000)


def _test_customer(request, customer_id):
    if not _TEST_ID.fullmatch(customer_id):
        raise HTTPException(404, "Test customer not found")
    return _state(request)


def _test_status(state, customer_id):
    customer = state.snapshot()["customers"].get(customer_id, {})
    status = state.customer_status(customer_id)
    return {"status": status,
            "paused_until": customer.get("paused_until") if status == "paused" else None}


@router.post("/api/test-customers")
async def create_test_customer(request: Request):
    """A new simulated Zalo customer for trying the bot; nothing is sent to Zalo."""
    _, state = _state(request)
    number = 1 + sum(1 for key in state.snapshot()["customers"] if is_test_customer(key))
    return {"id": TEST_CUSTOMER_PREFIX + secrets.token_hex(4), "name": f"Khách thử {number}"}


@router.post("/api/test-customers/clear")
async def clear_test_customers(request: Request):
    channel, _ = _state(request)
    removed = channel.clear_test_customers(getattr(request.app.state, "session_manager", None))
    return {"removed": removed}


@router.post("/api/test-customers/{customer_id}/messages")
async def send_test_message(request: Request, customer_id: str, body: TestMessage):
    channel, state = _test_customer(request, customer_id)
    await channel.simulate_customer_message(customer_id, body.name, body.content)
    return _test_status(state, customer_id)


@router.get("/api/test-customers/{customer_id}/messages")
async def test_messages(request: Request, customer_id: str, after: int = Query(0, ge=0)):
    channel, state = _test_customer(request, customer_id)
    return {"messages": channel.test_messages(customer_id, after), **_test_status(state, customer_id)}


class Pause(BaseModel):
    paused: StrictBool


class GroupSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed: StrictBool | None = None
    mode: Literal["mention", "ambient"] | None = None
    name: str | None = Field(default=None, max_length=100)


@router.get("/api/groups")
async def groups(request: Request):
    """Groups the bot was added to; the admin allows them one by one."""
    _, state = _state(request)
    rows = [dict(group, id=key) for key, group in state.snapshot().get("groups", {}).items()]
    rows.sort(key=lambda row: row["last_seen"], reverse=True)
    return {"groups": rows}


@router.post("/api/groups/{group_id}")
async def group_settings(request: Request, group_id: str, body: GroupSettings):
    _, state = _state(request)
    if not valid_group_id(group_id):
        raise HTTPException(400, "Invalid group id")
    if group_id not in state.snapshot().get("groups", {}):
        raise HTTPException(404, "Group not found")
    state.set_group(group_id, allowed=body.allowed, mode=body.mode, name=body.name)
    return {"group": dict(state.snapshot()["groups"][group_id], id=group_id)}


@router.get("/api/unanswered")
async def unanswered(request: Request):
    """Questions the bot could not answer from its knowledge, newest first, not yet handled."""
    _state(request)
    from nanobot.agent.unanswered import UnansweredLog
    log = UnansweredLog(request.app.state.config.workspace_path)
    return {"entries": await run_in_threadpool(log.entries)}


@router.post("/api/unanswered/{entry_id}/handled")
async def unanswered_handled(request: Request, entry_id: str):
    _state(request)
    if not re.fullmatch(r"[0-9a-f]{12}", entry_id):
        raise HTTPException(400, "Invalid entry id")
    from nanobot.agent.unanswered import UnansweredLog
    log = UnansweredLog(request.app.state.config.workspace_path)
    if not await run_in_threadpool(log.mark_handled, entry_id):
        raise HTTPException(404, "Entry not found")
    return {"handled": True}


@router.post("/api/pause-all")
async def pause_all(request: Request, body: Pause):
    _, state = _state(request)
    state.set_paused_all(body.paused)
    return {"paused_all": state.snapshot()["paused_all"]}


@router.get("/api/escalations")
async def escalations(request: Request, limit: int = Query(50, ge=1, le=500)):
    _, state = _state(request)
    rows = [
        dict(entry, customer_id=key, name=customer["name"])
        for key, customer in state.snapshot()["customers"].items()
        for entry in customer["escalations"]
    ]
    return {"escalations": sorted(rows, key=lambda row: row["at"], reverse=True)[:limit]}


@router.post("/api/telegram-test")
async def telegram_test(request: Request):
    _state(request)
    return {"published_to_bus": await _telegram(request, "[CSKH Zalo] Tin thử từ dashboard.")}


def _kb_error(exc):
    if isinstance(exc, KeyError):
        return HTTPException(404, "KB document not found")
    if isinstance(exc, (ConflictError, DisabledError)):
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc) if isinstance(exc, BrainMemoryError) else "Invalid document")


@router.get("/api/kb")
async def kb(request: Request):
    _state(request)
    agent = getattr(request.app.state, "agent", None)
    runtime = getattr(agent, "brain_runtime", None)
    try:
        return {
            "documents": await run_in_threadpool(
                kb_catalog.refresh_fact_counts,
                request.app.state.config.workspace_path,
                _brain_config(request),
                getattr(runtime, "worker", None),
                tolerate_errors=True,
            )
        }
    except (BrainMemoryError, ValueError, OSError) as exc:
        raise _kb_error(exc) from exc


@router.post("/api/kb/upload")
async def upload(request: Request, file: UploadFile = File(...), name: str | None = Form(None)):
    _state(request)
    try:
        data = await file.read(kb_catalog.MAX_UPLOAD_BYTES + 1)
        return await run_in_threadpool(
            kb_catalog.add_document,
            request.app.state.config.workspace_path,
            file.filename,
            data,
            name,
            config=_brain_config(request),
        )
    except (BrainMemoryError, ValueError, OSError, ImportError) as exc:
        raise _kb_error(exc) from exc
    finally:
        await file.close()


class Note(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=kb_catalog.NOTE_MAX_CHARS)


class FactIds(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_ids: list[str] = Field(min_length=1, max_length=50)


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=2000)


KB_TRY_SESSION = "kbtest:admin"  # the admin's own questions to the knowledge; not a customer


@router.post("/api/kb/teach")
async def kb_teach(request: Request, note: Note):
    """One admin note becomes knowledge at once; the reply lists the facts it may replace."""
    _state(request)
    agent = getattr(request.app.state, "agent", None)
    try:
        return await kb_catalog.add_note(
            request.app.state.config.workspace_path, _brain_config(request),
            getattr(agent, "brain_runtime", None), note.text,
        )
    except (BrainMemoryError, KeyError, ValueError, OSError) as exc:
        raise _kb_error(exc) from exc


@router.post("/api/kb/facts/retire")
async def kb_retire_facts(request: Request, body: FactIds):
    _state(request)
    try:
        return await run_in_threadpool(
            kb_catalog.retire_kb_facts, request.app.state.config.workspace_path, _brain_config(request), body.fact_ids,
        )
    except (BrainMemoryError, KeyError, ValueError, OSError) as exc:
        raise _kb_error(exc) from exc


@router.post("/api/kb/ask")
async def kb_ask(request: Request, question: Question):
    """Ask the bot as a customer would, without being one: the escalate tool refuses outside
    Zalo, so no Telegram alert, no pause, no customer record."""
    _state(request)
    agent = getattr(request.app.state, "agent", None)
    if agent is None or not hasattr(agent, "process_direct"):
        raise HTTPException(409, "Agent is not running")
    try:
        reply = await agent.process_direct(
            question.content, session_key=KB_TRY_SESSION, channel="kbtest", chat_id="admin",
        )
    except Exception as exc:
        raise HTTPException(502, public_error(exc)) from exc
    return {"reply": (reply.content or "") if reply is not None else "", "silent": reply is None}


@router.get("/api/kb/ask")
async def kb_ask_history(request: Request):
    _state(request)
    session = request.app.state.session_manager.get_or_create(KB_TRY_SESSION)
    return {"messages": [
        {"role": m["role"], "content": m["content"]} for m in session.messages
        if m.get("role") in {"user", "assistant"} and isinstance(m.get("content"), str)
        and m["content"].strip() != "__SILENT__"
    ]}


@router.post("/api/kb/ask/clear")
async def kb_ask_clear(request: Request):
    _state(request)
    sessions = request.app.state.session_manager
    session = sessions.get_or_create(KB_TRY_SESSION)
    session.clear()
    sessions.save(session)
    return {"cleared": True}


@router.get("/api/kb/{document_id}/facts")
async def kb_facts(request: Request, document_id: str):
    _state(request)
    try:
        return {"facts": await run_in_threadpool(
            kb_catalog.document_facts, request.app.state.config.workspace_path, _brain_config(request), document_id,
        )}
    except (BrainMemoryError, KeyError, ValueError, OSError) as exc:
        raise _kb_error(exc) from exc


@router.post("/api/kb/{document_id}/{action}")
async def kb_action(request: Request, document_id: str, action: str):
    _state(request)
    workspace, config = request.app.state.config.workspace_path, _brain_config(request)
    try:
        if action == "publish":
            agent = getattr(request.app.state, "agent", None)
            return await kb_catalog.publish_document(
                workspace, config, getattr(agent, "brain_runtime", None), document_id
            )
        if action == "revoke":
            return kb_catalog.revoke_document(workspace, config, document_id)
        if action == "relearn":
            agent = getattr(request.app.state, "agent", None)
            return await kb_catalog.relearn_document(
                workspace, config, getattr(agent, "brain_runtime", None), document_id
            )
        if action == "delete":
            kb_catalog.remove_document(workspace, document_id)
            return {"deleted": True}
        raise HTTPException(404, "Unknown KB action")
    except (BrainMemoryError, KeyError, ValueError, OSError) as exc:
        raise _kb_error(exc) from exc


class Persona(BaseModel):
    force: StrictBool = False


@router.post("/api/persona")
async def persona(request: Request, body: Persona):
    _state(request)
    try:
        installed = install_customer_service_soul(
            request.app.state.config.workspace_path, force=body.force
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(409, "Cannot replace SOUL.md safely") from exc
    return {"installed": installed, "needs_confirmation": not installed}


@router.get("/api/settings")
async def settings(request: Request):
    _state(request)
    return service_config(request.app.state.config).model_dump(by_alias=True)


@router.post("/api/settings")
async def update_settings(request: Request):
    _state(request)
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "Invalid JSON") from exc
    fields = ZaloCustomerServiceConfig.model_fields
    allowed = {
        key for name, field in fields.items() if name != "enabled" for key in (name, field.alias)
    }
    if not isinstance(body, dict) or body.keys() - allowed:
        raise HTTPException(
            400, "Only customerService settings may be changed; enabled requires restart"
        )
    return await save_zalo_config(request, {"customerService": body})
