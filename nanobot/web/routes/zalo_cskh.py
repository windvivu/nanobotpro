"""Authenticated admin surface for a public Zalo customer service instance."""

import json
import re
import secrets
import time
from collections import deque
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
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
from nanobot.utils.helpers import detect_image_mime, install_customer_service_soul
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


def _require_customer(request, customer_id):
    """Only customers who have written to the bot; asked on every poll and picture, so the state is
    not copied (custom)."""
    _, state = _state(request)
    if not state.has_customer(customer_id):
        raise HTTPException(404, "Customer not found")


def _require_group(request, group_id):
    """Only a group the bot was added to, as the Groups tab lists it (custom)."""
    _, state = _state(request)
    if not valid_group_id(group_id) or state.group(group_id) is None:
        raise HTTPException(404, "Group not found")


_MEDIA_MARKER = re.compile(r"\[(image|file|audio|video|voice|sticker|document)(?::[^\]]*)?\]")
# A picture the customer sent, as the session keeps it for the model: "[image: <saved file>]" (custom)
_IMAGE_MARKER = re.compile(r"\[image:\s*([^\]]+?)\s*\]")
# The same marker with its own line, for taking a drawn picture out of the text
_IMAGE_LINE = re.compile(r"[ \t]*\[image:\s*([^\]]+?)\s*\][ \t]*(?:\r?\n)?")
MEDIA_IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
                     ".webp": "image/webp"}


def _zalo_media_dir():
    """Where the Zalo channel saves what customers send: <data dir>/media/zalo."""
    from nanobot.config.paths import get_media_dir
    return get_media_dir("zalo")


def _customer_image_name(customer_id: str, name: str) -> bool:
    """One of this customer's pictures as the channel names them (<customer>_<ms>.<ext>): the page can
    ask for nothing else, and never another customer's. ".bin" is a picture Zalo sent as a file before
    the channel named those by their bytes; it is served only when its bytes are a picture (custom)."""
    return bool(re.fullmatch(re.escape(customer_id) + r"_\d{1,20}\.(jpg|jpeg|png|gif|webp|bin)", name or ""))


def _marker_name(path: str) -> str:
    return re.split(r"[\\/]", path)[-1]


def _images(content, customer_id: str, media) -> list[dict]:
    """The pictures of one message, the customer's or staff's: their file names (not the server
    path) and whether still in the media folder."""
    if isinstance(content, list):
        content = "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
    if not isinstance(content, str) or "[image:" not in content:
        return []
    found = []
    for match in _IMAGE_MARKER.finditer(content):
        name = _marker_name(match.group(1))
        if _customer_image_name(customer_id, name) and name not in {image["name"] for image in found}:
            found.append({"name": name, "available": (media / name).is_file()})
    return found


def _shown(content, workspace, pictures=()):
    """What the staff member reads: a message stored as blocks becomes its text; a media
    marker loses the path it carries for the model (a server path is not for the page).
    A picture the page draws (pictures: its file names) leaves the text; one it cannot draw
    stays as "[image]", so staff still sees that something was sent (custom)."""
    if isinstance(content, list):
        parts = [block.get("text", "") if isinstance(block, dict) else str(block) for block in content]
        content = "\n".join(part for part in parts if part)
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    if pictures:
        content = _IMAGE_LINE.sub(lambda m: "" if _marker_name(m.group(1)) in pictures else m.group(0),
                                  content).strip()
    from nanobot.security.public_output import redact_workspace
    return redact_workspace(_MEDIA_MARKER.sub(lambda m: f"[{m.group(1)}]", content), workspace)


# What the channel puts before a group message for the model, and the message a reply quotes (custom)
_GROUP_NOTE = re.compile(r"\[GROUP\b[^\]]*\]\s*")
_QUOTE = re.compile(r'\[Quoted: "(.*?)"\]\s*', re.S)


def _group_line(text: str, sender: str | None) -> dict:
    """A member's message in a group as staff reads it (custom): without the notes for the model;
    who wrote it, and what it answers, apart (the note of an unmentioned message already names the
    sender before the text)."""
    text = _GROUP_NOTE.sub("", text).strip()
    line = {}
    if quoted := _QUOTE.match(text):
        line["quote"] = " ".join(quoted.group(1).split())[:160]
        text = text[quoted.end():]
    if sender:
        line["sender"] = sender
        if text.startswith(sender + ": "):
            text = text[len(sender) + 2:]
    line["content"] = text.strip()
    return line


def _part_line(part: dict) -> str:
    """One message of a merged group turn as staff reads it: "↪ quote" above "Sender: words"."""
    quote = " ".join(str(part.get("quote") or "").split())[:160]
    words = str(part.get("text") or "")
    line = f"{part['sender']}: {words}" if part.get("sender") else words
    return f"\u21aa {quote}\n{line}" if quote else line


def _transcript(request, conversation_id, limit, *, group=False):
    """A customer's conversation with the bot, or a group's (custom), as the CSKH page shows it."""
    (_require_group if group else _require_customer)(request, conversation_id)
    media = _zalo_media_dir()  # once per read, not per message (custom)
    key = f"zalo:{conversation_id}"
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
                if item.get("role") == "assistant" and not str(item.get("content") or "").strip():
                    continue  # the bot only called a tool (knowledge search, escalation): nothing was sent
                if item.get("opening_note"):
                    continue  # for the model only: staff opened the group's conversation (custom)
                if item.get("role") in {"user", "assistant"}:
                    # Pictures, the customer's or what staff sent, are drawn: their markers leave the
                    # text, and "[Nhân viên] [image: …]" keeps its prefix (custom)
                    images = _images(item.get("content"), conversation_id, media)
                    row = {"role": item["role"], "timestamp": item.get("timestamp", ""),
                           "content": _shown(item.get("content", ""), request.app.state.config.workspace_path,
                                             {image["name"] for image in images})}
                    if images:
                        row["images"] = images
                    if group and item["role"] == "user":
                        if isinstance(item.get("parts"), list):  # a merged turn: each part as written
                            row["content"] = _shown("\n".join(_part_line(part) for part in item["parts"]
                                                              if isinstance(part, dict)),
                                                    request.app.state.config.workspace_path)
                        elif isinstance(item.get("text"), str):  # kept apart since review 15 #6
                            row["content"] = _shown(item["text"], request.app.state.config.workspace_path)
                            for field, value in (("sender", item.get("sender_name")), ("quote", item.get("quote"))):
                                if isinstance(value, str) and value:
                                    row[field] = " ".join(value.split())[:160] if field == "quote" else value
                        else:  # older lines, and turns merged from several messages
                            row.update(_group_line(row["content"], item.get("sender_name")))
                    for key in ("reply_ms", "searches"):  # how long the customer waited (custom)
                        if isinstance(item.get(key), int) and not isinstance(item.get(key), bool) and item[key] >= 0:
                            row[key] = item[key]
                    hold = item.get("hold")  # the hold message sent before this answer
                    if isinstance(hold, dict) and isinstance(hold.get("text"), str) and isinstance(hold.get("after_ms"), int):
                        row["hold"] = {"text": _shown(hold["text"], request.app.state.config.workspace_path),
                                       "after_ms": max(0, hold["after_ms"])}
                    rows.append(row)
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


LIVE_CHAT_SECONDS = 300  # a customer who wrote this recently is "chatting with the bot now" (custom)


def _chatting(key: str, customer: dict, now: float) -> bool:
    """A real customer (not a page test) who wrote to the bot privately within LIVE_CHAT_SECONDS and
    is not blocked. A member writing in a group is not: the Groups tab shows a group's activity."""
    return (not is_test_customer(key) and customer.get("status") != "blocked"
            and (customer.get("last_private_at") or 0) >= now - LIVE_CHAT_SECONDS)


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
    now = time.time()
    groups = snapshot.get("groups", {})
    groups_waiting = sum(_group_needs_attention(state, key, group) for key, group in groups.items())
    # Groups the bot is in that the admin has not decided on yet, newest first (custom)
    new_groups = sorted(((group.get("joined_at") or group["first_seen"], group.get("name") or key)
                         for key, group in groups.items() if _group_pending(group)), reverse=True)
    chatting = sorted(((c.get("last_private_at") or 0, c["name"] or key) for key, c in snapshot["customers"].items()
                       if _chatting(key, c, now)), reverse=True)
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
            "groups": groups_waiting + len(new_groups),
            "groups_waiting": groups_waiting,  # groups handed to staff (custom): the alert counts them too
            "groups_new": len(new_groups),  # groups waiting for a decision: until allowed or set aside
        },
        # Who is talking to the bot right now: the page shows it on the Customers tab from any tab,
        # and on the Groups tab the allowed groups where someone wrote just now (custom)
        "live": {"chatting": len(chatting), "names": [name for _, name in chatting[:5]],
                 "window_s": LIVE_CHAT_SECONDS,
                 "groups": sum(bool(group.get("allowed")) and _group_active(group, now) for group in groups.values()),
                 "new_groups": [name for _, name in new_groups[:5]]},
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
    if status not in {"", "active", "paused", "blocked", "waiting", "escalated"}:
        raise HTTPException(400, "Invalid customer status")
    rows, now = [], time.time()
    for key, customer in state.snapshot()["customers"].items():
        effective = state.customer_status(key)
        # waiting: handed to staff and not resumed yet; escalated: handed over at least once
        matches = {"waiting": lambda: _needs_attention(state, key, customer),
                   "escalated": lambda: bool(customer["escalations"])}.get(status, lambda: status == effective)
        if (status and not matches()) or q.casefold() not in (
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
                "chatting": _chatting(key, customer, now),
                # When a paused customer can write to the bot again (None: until resumed).
                "paused_until": customer.get("paused_until") if effective == "paused" else None,
            }
        )
    return {"customers": sorted(rows, key=lambda row: row["last_seen"], reverse=True)[:limit]}


def _picture_file(customer_id: str, name: str):
    """The file and image type of one of this customer's pictures, or 404: a regular file directly in
    the media folder; a ".bin" only when its bytes are a picture (custom)."""
    if not _customer_image_name(customer_id, name):
        raise HTTPException(404, "Image not found")
    media = _zalo_media_dir().resolve()
    path = media / name
    if path.is_symlink() or not path.is_file() or path.resolve().parent != media:
        raise HTTPException(404, "Image not found")
    mime = MEDIA_IMAGE_TYPES.get(path.suffix.lower())
    if mime is None:
        with path.open("rb") as stream:
            mime = detect_image_mime(stream.read(16))
    if mime is None:
        raise HTTPException(404, "Image not found")
    return path, mime


def _picture_response(path, mime) -> FileResponse:
    return FileResponse(path, media_type=mime,
                        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


@router.get("/api/customers/{customer_id}/media/{name}")
async def customer_media(request: Request, customer_id: str, name: str):
    """A picture this customer sent, or staff sent them, for the conversation on this page (custom).
    Only a file named for this customer in the Zalo media folder, served as an image."""
    _require_customer(request, customer_id)
    return _picture_response(*await run_in_threadpool(_picture_file, customer_id, name))


@router.get("/api/groups/{group_id}/media/{name}")
async def group_media(request: Request, group_id: str, name: str):
    """A picture sent in this group, by a member or by staff (custom): named for the group."""
    _require_group(request, group_id)
    return _picture_response(*await run_in_threadpool(_picture_file, group_id, name))


@router.get("/api/customers/{customer_id}/transcript")
async def transcript(request: Request, customer_id: str, limit: int = Query(50, ge=1, le=200)):
    # Off the event loop: the page polls this every 2 s for each open conversation (custom)
    return {"messages": await run_in_threadpool(_transcript, request, customer_id, limit)}


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
        # A picture drawn on the page is "[image]" again in the notice: a line is never left empty (custom)
        [{**row, "content": " ".join([row["content"]] + ["[image]"] * len(row.get("images", []))).strip()}
         for row in _transcript(request, customer_id, 10)],
    )
    delivered = await _telegram(request, content)
    state.record_escalation(
        customer_id, "other", delivered, time.time() + service.pause_after_escalation_minutes * 60
    )
    return {"published_to_bus": delivered}


STAFF_PREFIX = "[Nhân viên] "
# Before staff's first words in a group whose session has no customer line the model reads yet: the
# history starts at a user line, so those words were dropped after Resume (review 15, last case). A
# channel note in the customer's place keeps them; it is a [GROUP ...] note, so the knowledge search
# and the Telegram alert leave it out, and the page does not show it (custom)
GROUP_STAFF_OPENING = "[GROUP — staff wrote first in this group; their messages follow.]"
STAFF_PICTURES_MAX = 5  # per send; Zalo gets each picture as its own message (custom)
STAFF_PICTURE_MAX_BYTES = 10 * 1024 * 1024
_PICTURE_OWNER = re.compile(r"[A-Za-z0-9-]{1,64}")  # a customer id that can start a media file name


class StaffReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=2000)


_PICTURE_SUFFIX = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
_KEEP = object()


class _StaffHolds:
    """The hold staff sends placed on a conversation, while any of them is on its way (custom). A send
    Zalo took none of puts the hold back only when it is the last one out and none went through: two
    failed sends from two tabs left the first one's hold behind (review 15 #3). A handoff, Resume or
    block made meanwhile changes the mark, and the hold is then left as it is."""

    def __init__(self) -> None:
        self._open: dict[tuple, dict] = {}

    def begin(self, key: tuple, mark) -> None:
        batch = self._open.setdefault(key, {"before": mark, "held": mark, "sending": 0, "delivered": False})
        batch["sending"] += 1

    def placed(self, key: tuple, mark) -> None:
        self._open[key]["held"] = mark

    def end(self, key: tuple, delivered: bool, mark):
        """The mark to put back, or _KEEP."""
        batch = self._open[key]
        batch["sending"] -= 1
        batch["delivered"] = batch["delivered"] or delivered
        if batch["sending"]:
            return _KEEP
        del self._open[key]
        return _KEEP if batch["delivered"] or mark != batch["held"] else batch["before"]


_HOLDS = _StaffHolds()


def _save_pictures(customer_id: str, pictures) -> list:
    """Staff's pictures go where the channel keeps the customer's own, named the same way
    (<customer>_<ms>.<ext>): the conversation shows them and retention removes them. Each name is
    taken by an exclusive create, so a send from a second tab never overwrites one (custom)."""
    media, stamp, saved = _zalo_media_dir(), int(time.time() * 1000), []
    try:
        for suffix, data in pictures:
            while True:
                path = media / f"{customer_id}_{stamp}{suffix}"
                stamp += 1
                try:
                    stream = path.open("xb")
                except FileExistsError:
                    continue
                saved.append(path)
                with stream:
                    stream.write(data)
                break
    except OSError:
        for path in saved:
            path.unlink(missing_ok=True)
        raise
    return saved


async def _send_as_staff(request, conversation_id: str, text: str, pictures: list | None = None, *,
                         group: bool = False) -> dict:
    """Answer a customer, or a group (custom), as staff: the bot's Zalo account sends the text, then
    each picture, and the bot stays quiet there until Resume or the pause ends. Each part goes into
    the bot's conversation as soon as Zalo confirms it, so what the customer writes meanwhile stays
    after what it answers. Nothing delivered raises; a picture Zalo did not confirm ends the send
    there, and the reply says how many pictures went out (custom)."""
    channel, state = _state(request)
    if group and not state.group_access(conversation_id)[0]:
        raise HTTPException(409, "The bot does not serve this group; allow it on the Groups tab first")
    if not is_test_customer(conversation_id) and channel.get_status().get("status") != "connected":
        raise HTTPException(409, "Zalo is not connected; the reply was not sent")
    try:
        paths = await run_in_threadpool(_save_pictures, conversation_id, pictures) if pictures else []
    except OSError as exc:
        public_error(exc)
        raise HTTPException(500, "The pictures could not be saved; nothing was sent") from exc
    finally:
        if pictures:
            pictures.clear()  # on disk now: up to 50 MB is not held through a send of minutes
    # Hold first, so a message the customer sends while the reply is on its way is kept for
    # staff rather than answered by the bot; undo the hold if Zalo did not take the reply.
    pause = service_config(request.app.state.config).pause_after_escalation_minutes * 60
    # The mark: the hold, and what tells a handoff, a Resume or a block made meanwhile
    if group:
        def mark():
            found = state.group(conversation_id) or {}
            return (found.get("held_until"), (found.get("escalations") or [{}])[-1].get("at"),
                    found.get("escalation_cleared_at"))

        def hold():
            state.hold_group_for_staff(conversation_id, time.time() + pause)

        def undo(before):
            state.set_group_hold(conversation_id, before[0])
    else:
        def mark():
            found = state.snapshot()["customers"].get(conversation_id, {})
            return (found.get("status", "active"), found.get("paused_until"),
                    (found.get("escalations") or [{}])[-1].get("at"), found.get("escalation_cleared_at"))

        def hold():
            state.hold_for_staff(conversation_id, time.time() + pause)

        def undo(before):
            if before[0] != "blocked":
                state.set_status(conversation_id, before[0], paused_until=before[1])
    sessions = getattr(request.app.state, "session_manager", None)

    def record(line: str) -> None:
        if sessions is None:
            return
        try:
            session = sessions.get_or_create(f"zalo:{conversation_id}")
            if group and not any(message["role"] == "user" for message in session.get_history()):
                session.add_message("user", GROUP_STAFF_OPENING, opening_note=True)
            session.add_message("assistant", STAFF_PREFIX + line)
            sessions.save(session)
        except Exception as exc:  # it went out: a failed save must not report it as unsent
            public_error(exc)

    metadata = {"thread_type": "Group" if group else "User", "_staff_reply": True, "_confirm": True}
    text_sent, sent, failure = False, 0, None
    key = (id(state), "group" if group else "customer", conversation_id)
    _HOLDS.begin(key, mark())
    try:
        hold()
        _HOLDS.placed(key, mark())
        try:
            # _confirm: no ack from Zalo in time, or not connected, raises; what went before stays sent.
            if text:
                await channel.send(OutboundMessage(channel="zalo", chat_id=conversation_id, content=text,
                                                   metadata=dict(metadata)))
                text_sent = True
                record(text)
            for path in paths:
                await channel.send(OutboundMessage(channel="zalo", chat_id=conversation_id, content="",
                                                   media=[str(path)], metadata=dict(metadata)))
                sent += 1
                record(f"[image: {path.name}]")  # the name only: no server path for the model
        except Exception as exc:
            public_error(exc)
            failure = exc
    finally:
        if (before := _HOLDS.end(key, text_sent or sent > 0, mark())) is not _KEEP:
            undo(before)
    for path in paths[sent:]:
        path.unlink(missing_ok=True)  # not sent: nothing in the conversation points at it
    if not text_sent and not sent:
        raise HTTPException(502, "Zalo did not confirm delivery; the reply was not recorded") from failure
    if group:
        status, until = state.group_status(conversation_id), mark()[0]
    else:
        status, until = state.customer_status(conversation_id), mark()[1]
    # unconfirmed: no ack in time, so the next picture may still reach the customer
    return {"sent": True, "status": status, "paused_until": until,
            "images_sent": sent, "unconfirmed": isinstance(failure, TimeoutError)}


async def _read_pictures(owner_id: str, files: list[UploadFile], content: str) -> tuple[str, list]:
    """The text and pictures of a staff send, all checked before anything goes out (custom)."""
    if not _PICTURE_OWNER.fullmatch(owner_id):
        raise HTTPException(400, "Pictures cannot be sent here")
    text = content.replace("\r\n", "\n").strip()  # a form sends line breaks as CRLF; /reply has LF
    if len(text) > 2000:
        raise HTTPException(400, "Reply is longer than 2000 characters")
    if not 1 <= len(files) <= STAFF_PICTURES_MAX:
        raise HTTPException(400, f"Send 1 to {STAFF_PICTURES_MAX} pictures at a time")
    pictures = []
    try:
        for file in files:
            label = (file.filename or "picture")[:80]
            data = await file.read(STAFF_PICTURE_MAX_BYTES + 1)
            if len(data) > STAFF_PICTURE_MAX_BYTES:
                raise HTTPException(413, f"{label} is larger than 10 MB")
            if (mime := detect_image_mime(data[:16])) is None:
                raise HTTPException(400, f"{label} is not a JPG, PNG, WebP or GIF picture")
            pictures.append((_PICTURE_SUFFIX[mime], data))  # the type from its bytes, not the name it came with
    finally:
        for file in files:
            await file.close()
    return text, pictures


@router.post("/api/customers/{customer_id}/reply")
async def staff_reply(request: Request, customer_id: str, body: StaffReply):
    _require_customer(request, customer_id)  # only customers who have written to the bot
    text = body.content.strip()
    if not text:
        raise HTTPException(400, "Reply is empty")
    return await _send_as_staff(request, customer_id, text)


@router.post("/api/customers/{customer_id}/reply-images")
async def staff_reply_images(request: Request, customer_id: str, files: list[UploadFile] = File(...),
                             content: str = Form("")):
    """Pictures from staff (picked or pasted on the page), with an optional text sent first (custom)."""
    _require_customer(request, customer_id)
    text, pictures = await _read_pictures(customer_id, files, content)
    return await _send_as_staff(request, customer_id, text, pictures)


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
    dismissed: StrictBool | None = None  # set aside: the bot stays silent there, no longer waiting (custom)


def _group_needs_attention(state, key, group) -> bool:
    """Handed to staff and not yet resumed: a staff member should look at this group (custom)."""
    return state.group_status(key) == "paused" and any(
        entry["at"] > (group.get("escalation_cleared_at") or 0) for entry in group.get("escalations", []))


def _group_pending(group) -> bool:
    """The bot is in a group the admin has neither allowed nor set aside: it waits for a decision
    (custom), however long ago the bot was added; taken out of the group, it no longer does."""
    return not group.get("allowed") and not group.get("dismissed") and not group.get("left_at")


def _group_active(group, now: float) -> bool:
    """A member wrote in the group within LIVE_CHAT_SECONDS (custom)."""
    return (group.get("last_message_at") or 0) >= now - LIVE_CHAT_SECONDS


def _group_row(state, key, group) -> dict:
    status = state.group_status(key)
    return dict(group, id=key, status=status, escalations=len(group.get("escalations", [])),
                paused_until=group.get("held_until") if status == "paused" else None,
                attention=_group_needs_attention(state, key, group),
                pending=_group_pending(group), active=_group_active(group, time.time()))


@router.get("/api/groups")
async def groups(request: Request):
    """Groups the bot was added to; the admin allows them one by one."""
    _, state = _state(request)
    rows = [_group_row(state, key, group) for key, group in state.snapshot().get("groups", {}).items()]
    rows.sort(key=lambda row: row["last_seen"], reverse=True)
    return {"groups": rows}


@router.post("/api/groups/{group_id}")
async def group_settings(request: Request, group_id: str, body: GroupSettings):
    _, state = _state(request)
    if not valid_group_id(group_id):
        raise HTTPException(400, "Invalid group id")
    if group_id not in state.snapshot().get("groups", {}):
        raise HTTPException(404, "Group not found")
    state.set_group(group_id, allowed=body.allowed, mode=body.mode, name=body.name, dismissed=body.dismissed)
    return {"group": _group_row(state, group_id, state.group(group_id))}


@router.get("/api/groups/{group_id}/transcript")
async def group_transcript(request: Request, group_id: str, limit: int = Query(50, ge=1, le=200)):
    """The group's conversation with the bot, each member's line with its sender (custom)."""
    return {"messages": await run_in_threadpool(lambda: _transcript(request, group_id, limit, group=True))}


@router.post("/api/groups/{group_id}/reply")
async def group_reply(request: Request, group_id: str, body: StaffReply):
    """Staff answer in the group through the bot's Zalo account; the bot gives way there (custom)."""
    _require_group(request, group_id)
    text = body.content.strip()
    if not text:
        raise HTTPException(400, "Reply is empty")
    return await _send_as_staff(request, group_id, text, group=True)


@router.post("/api/groups/{group_id}/reply-images")
async def group_reply_images(request: Request, group_id: str, files: list[UploadFile] = File(...),
                             content: str = Form("")):
    _require_group(request, group_id)
    text, pictures = await _read_pictures(group_id, files, content)
    return await _send_as_staff(request, group_id, text, pictures, group=True)


@router.post("/api/groups/{group_id}/resume")
async def group_resume(request: Request, group_id: str):
    """The bot answers in the group again, and may hand it over again at once (custom)."""
    _require_group(request, group_id)
    _, state = _state(request)
    state.resume_group(group_id)
    return {"group": _group_row(state, group_id, state.group(group_id))}


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
        documents = await run_in_threadpool(
            kb_catalog.refresh_fact_counts,
            request.app.state.config.workspace_path,
            _brain_config(request),
            getattr(runtime, "worker", None),
            tolerate_errors=True,
        )
        for doc in documents:  # taught notes are listed apart from documents (custom)
            doc["kind"] = kb_catalog.document_kind(doc)
        return {"documents": documents}
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
    origin: Literal["page", "unanswered", "conversation"] = "page"  # where on the page it was taught


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
            getattr(agent, "brain_runtime", None), note.text, origin=note.origin,
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
    # How long the answer took and how often the bot searched, as the conversations show (custom)
    sessions = getattr(agent, "sessions", None) or request.app.state.session_manager
    last = next((m for m in reversed(sessions.get_or_create(KB_TRY_SESSION).messages)
                 if m.get("role") == "assistant"), {}) if reply is not None else {}
    return {"reply": (reply.content or "") if reply is not None else "", "silent": reply is None, **_timing(last)}


def _timing(message: dict) -> dict:
    return {key: message[key] for key in ("reply_ms", "searches")
            if isinstance(message.get(key), int) and not isinstance(message.get(key), bool) and message[key] >= 0}


@router.get("/api/kb/ask")
async def kb_ask_history(request: Request):
    _state(request)
    session = request.app.state.session_manager.get_or_create(KB_TRY_SESSION)
    return {"messages": [
        {"role": m["role"], "content": m["content"], **_timing(m)} for m in session.messages
        if m.get("role") in {"user", "assistant"} and isinstance(m.get("content"), str)
        and m["content"].strip() not in {"__SILENT__", ""}  # empty: the bot only called a tool
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
        if action == "revoke":  # moves fact files and rebuilds the index: off the event loop (custom, review)
            return await run_in_threadpool(kb_catalog.revoke_document, workspace, config, document_id)
        if action == "relearn":
            agent = getattr(request.app.state, "agent", None)
            return await kb_catalog.relearn_document(
                workspace, config, getattr(agent, "brain_runtime", None), document_id
            )
        if action == "delete":
            await run_in_threadpool(kb_catalog.remove_document, workspace, document_id)
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
