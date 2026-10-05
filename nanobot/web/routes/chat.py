"""Web Chat — real-time chat via WebSocket."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from loguru import logger
from starlette.datastructures import UploadFile

from nanobot.config.paths import get_media_dir
from nanobot.providers.transcription import (
    GroqTranscriptionProvider,
    TranscriptionError,
    is_certificate_error,
)
from nanobot.utils.document import extract_text
from nanobot.utils.helpers import detect_image_mime, strip_think
from nanobot.web.auth import is_authenticated

router = APIRouter()

# Map tool names → friendly Vietnamese descriptions
_TOOL_LABELS = {
    "web_search": "🔍 Đang tìm kiếm trên web...",
    "read_url": "🌐 Đang đọc trang web...",
    "read_file": "📄 Đang đọc file...",
    "write_file": "📝 Đang ghi file...",
    "list_files": "📂 Đang duyệt thư mục...",
    "execute_command": "⚙️ Đang chạy lệnh...",
    "send_message": "💬 Đang gửi tin nhắn...",
    "spawn_agent": "🤖 Đang khởi tạo sub-agent...",
}


def _friendly_tool_hint(raw: str) -> str:
    """Convert raw tool hint like 'web_search(\"query\")' to friendly text."""
    for tool_name, label in _TOOL_LABELS.items():
        if tool_name in raw:
            return label
    # Fallback: strip function syntax → "Đang xử lý..."
    return "⏳ Đang xử lý..."


def _get_workspace(request_or_app) -> Path:
    if hasattr(request_or_app, "app"):
        return request_or_app.app.state.config.workspace_path
    return request_or_app.state.config.workspace_path


def _get_bot_name(workspace: Path) -> str:
    """Get bot display name from active profile, fallback to 'Nanobot'."""
    return _get_active_profile_name(workspace)


def _get_active_profile_name(workspace: Path) -> str:
    """Get active profile name."""
    pfile = workspace / "profiles" / "profiles.json"
    if not pfile.exists():
        return "Default"
    try:
        data = json.loads(pfile.read_text(encoding="utf-8"))
        active_id = data.get("active")
        if not active_id:
            return "Default"
        for p in data.get("profiles", []):
            if p["id"] == active_id:
                return p["name"]
    except (json.JSONDecodeError, OSError):
        pass
    return "Default"


def _load_session_history(workspace: Path, session_key: str, limit: int = 50) -> list[dict]:
    """Load recent messages from session file for display."""
    sessions_dir = workspace / "sessions"
    # session key format: webchat:web_user -> file: webchat_web_user.jsonl
    filename = session_key.replace(":", "_") + ".jsonl"
    session_file = sessions_dir / filename
    if not session_file.exists():
        return []

    messages = []
    try:
        lines = session_file.read_text(encoding="utf-8").strip().split("\n")
        for line in lines[-limit:]:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
                role = msg.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                content = msg.get("content", "")
                if role == "user" and isinstance(content, str) and content.startswith("[Subagent '"):
                    continue  # a sub-agent's report to the bot, kept as context; the bot's reply follows
                text, media = _display_content(content)
                if text or media:
                    messages.append({"role": role, "content": text, **({"media": media} if media else {})})
                if role == "assistant":
                    messages.extend(_sent_files(msg, workspace, refresh=False))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return messages


def _webchat_session_key(agent=None) -> str:
    """Return the session key Web Chat should use for display and processing."""
    if agent and getattr(agent, "_unified_session", False):
        return "unified:default"
    return "webchat:web_user"


# ── Attachments ─────────────────────────────────────────────
# Uploads live in <data dir>/media/webchat/<upload id>/<name>. The media dir is readable
# by the agent's read_file even when tools are restricted to the workspace.
_UPLOAD_MAX_BYTES = 8 * 1024 * 1024  # per file; the chat page reads both limits from here
_UPLOAD_MAX_FILES = 5  # per upload and per message
_INLINE_MAX_CHARS = 20_000  # readable files up to this size go into the message itself
_MEDIA_KEEP_S = 24 * 3600  # uploads and copies of sent files are deleted a day after they were stored
_PURGE_EVERY_S = 3600
_ATTACHED = re.compile(  # how _with_attachments names files in the message text
    r"\[Attached file: (?P<path>[^\]\n]+)\]\n.*?\n\[End of attached file: (?P=path)\]"
    r"|\[Attached file saved at: (?P<saved>[^\]\n]+?); read it with read_file\]",
    re.DOTALL,
)
_IMAGE_PLACEHOLDER = re.compile(r"\[image(?:: (?P<path>[^\]\n]+))?\]")  # how sessions store images
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                     *(f"LPT{i}" for i in range(1, 10))}


def _safe_filename(name: str | None) -> str:
    """A readable name for an uploaded file that stays inside its folder on every OS."""
    name = re.sub(r"[^\w.\- ()]+", "_", Path((name or "").replace("\\", "/")).name).strip(" .")
    stem, suffix = Path(name).stem[:100], Path(name).suffix[:16]
    if stem.upper() in _WINDOWS_RESERVED:
        stem = "_" + stem
    return (stem + suffix) or "file"


def _free_path(folder: Path, name: str) -> Path:
    """`folder/name`, numbered when an earlier file of the same upload took that name."""
    path, n = folder / name, 1
    while path.exists():
        n += 1
        path = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
    return path


async def _save_upload(upload: UploadFile, path: Path) -> None:
    """Copy one upload to `path` in chunks, refusing files over the size limit."""
    size = 0
    with path.open("xb") as out:
        while chunk := await upload.read(1 << 20):
            size += len(chunk)
            if size > _UPLOAD_MAX_BYTES:
                raise ValueError(f"{upload.filename}: quá {_UPLOAD_MAX_BYTES // (1024 * 1024)} MB")
            out.write(chunk)


def _media_file(rel: str, root: Path) -> Path | None:
    """The file `rel` names inside `root`, or None if it is missing or would leave `root`."""
    try:
        base = root.resolve()
        path = (base / rel).resolve()
        path.relative_to(base)
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


def _image_mime(path: Path) -> str | None:
    """MIME type of a PNG/JPEG/GIF/WebP image (what models and browsers take), from its bytes."""
    try:
        with path.open("rb") as f:
            return detect_image_mime(f.read(16))
    except OSError:
        return None


def _attachment_info(path: Path | None, kind: str) -> dict:
    """What the page shows for one attachment; `url` only for files that still exist under the
    media dir (old ones are purged after a day: the page then shows just the name)."""
    info = {"name": path.name if path else "image", "kind": kind, "url": None}
    if path is not None:
        try:
            rel = path.resolve().relative_to(get_media_dir().resolve())
        except (OSError, ValueError):
            return info
        if path.is_file():
            info["url"] = "/chat/media/" + quote(rel.as_posix())
    return info


def _purge_old_media(now: float | None = None) -> int:
    """Delete Web Chat uploads and copies of sent files stored more than _MEDIA_KEEP_S ago.

    Each upload (media/webchat/<id>/) and each sent-file copy (media/webchat/sent/<hash>/) is a
    folder; its age is that of its newest file, since a re-sent file refreshes its copy.
    """
    root = get_media_dir("webchat")
    cutoff = (time.time() if now is None else now) - _MEDIA_KEEP_S
    removed = 0
    for parent in (root, root / "sent"):
        if not parent.is_dir():
            continue
        for folder in parent.iterdir():
            if not folder.is_dir() or folder == root / "sent":
                continue
            try:
                stored = max((f.stat().st_mtime for f in folder.iterdir()), default=folder.stat().st_mtime)
            except OSError:
                continue
            if stored < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
    return removed


async def purge_old_attachments_forever() -> None:
    """Run _purge_old_media now and then every _PURGE_EVERY_S; start_web runs it with the server."""
    while True:
        try:
            removed = await asyncio.to_thread(_purge_old_media)
            if removed:
                logger.info("[WebChat] Deleted {} attachment folder(s) older than a day", removed)
        except Exception as e:
            logger.warning("[WebChat] Attachment cleanup failed: {}", e)
        await asyncio.sleep(_PURGE_EVERY_S)


def _with_attachments(text: str, files: list[Path]) -> tuple[str, list[str]]:
    """The message the agent gets for `text` plus `files`, and the images it should see.

    Readable files up to _INLINE_MAX_CHARS are put in the message; larger or unreadable
    ones are named by path for read_file, so they do not sit in every later prompt.
    """
    images, parts = [], [text] if text else []
    for path in files:
        if _image_mime(path):
            images.append(str(path))
            continue
        extracted = extract_text(path)
        if extracted and not extracted.startswith("[error:") and len(extracted) <= _INLINE_MAX_CHARS:
            parts.append(f"[Attached file: {path}]\n{extracted}\n[End of attached file: {path}]")
        else:
            parts.append(f"[Attached file saved at: {path}; read it with read_file]")
    return "\n\n".join(parts), images


def _display_content(content) -> tuple[str, list[dict]]:
    """Text and attachments to show for a stored message (a string or a list of blocks)."""
    media: list[dict] = []
    if isinstance(content, list):
        texts = []
        for block in content:
            text = block.get("text") if isinstance(block, dict) else None
            if not isinstance(text, str):
                continue
            image = _IMAGE_PLACEHOLDER.fullmatch(text.strip())
            if image:
                media.append(_attachment_info(Path(image["path"]) if image["path"] else None, "image"))
            else:
                texts.append(text)
        content = "\n\n".join(texts)
    if not isinstance(content, str):
        return "", media

    def _collect(match: re.Match) -> str:
        media.append(_attachment_info(Path(match["path"] or match["saved"]), "file"))
        return ""

    return _ATTACHED.sub(_collect, content).strip(), media


# Files the agent sends with the message tool. The gateway has no Web Chat channel (adding one
# would make heartbeats pick Web Chat and get lost when no page is open), so the route delivers
# them from the turn's saved tool calls, and the history rebuilds them the same way.
_SHARE_MAX_BYTES = 100 * 1024 * 1024  # bigger files are listed without a link


def _shared_path(source: Path) -> Path:
    """Where the page gets a file the agent sent: the file itself under the media dir, otherwise
    a copy in media/webchat/sent/<hash of its path>/, so a link keeps working after it changes."""
    resolved = source.resolve()
    try:
        resolved.relative_to(get_media_dir().resolve())
        return resolved
    except ValueError:
        digest = hashlib.sha1(os.path.normcase(str(resolved)).encode()).hexdigest()[:12]
        return get_media_dir("webchat") / "sent" / digest / _safe_filename(source.name)


def _share_file(path: str, workspace: Path, *, refresh: bool) -> dict:
    """Attachment info for a file the agent sent, copying it where /chat/media serves it.

    refresh=True right after the turn that sent it (the agent may have rewritten the file);
    the history only copies files that have no copy yet.
    """
    source = Path(path) if Path(path).is_absolute() else workspace / path
    try:
        target = _shared_path(source)
        stale = refresh or not target.is_file()
        if target != source.resolve() and stale and source.is_file() \
                and source.stat().st_size <= _SHARE_MAX_BYTES:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    except OSError:
        return {"name": source.name, "kind": "file", "url": None}
    if not target.is_file():
        return {"name": source.name, "kind": "file", "url": None}
    return _attachment_info(target, "image" if _image_mime(target) else "file")


def _sent_files(message: dict, workspace: Path, *, refresh: bool) -> list[dict]:
    """Bot bubbles for files one stored assistant message sent to Web Chat with the message tool."""
    bubbles = []
    for call in message.get("tool_calls") or []:
        fn = call.get("function") if isinstance(call, dict) else None
        if not isinstance(fn, dict) or fn.get("name") != "message":
            continue
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                continue
        if not isinstance(args, dict) or args.get("channel") not in (None, "", "webchat"):
            continue  # sent to another channel, e.g. Zalo in a unified session
        paths = [p for p in args.get("media") or [] if isinstance(p, str)]
        if paths:
            bubbles.append({"role": "assistant", "content": strip_think(args.get("content") or ""),
                            "media": [_share_file(p, workspace, refresh=refresh) for p in paths]})
    return bubbles


_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _cat_assets_version() -> int:
    """Changes whenever static/chat-cat.js, chat-cat.css or chat-voice.js change, so browsers do not
    keep an old copy (StaticFiles sends no Cache-Control, which lets browsers reuse a file for a while)."""
    try:
        return int(max((_STATIC_DIR / name).stat().st_mtime
                       for name in ("chat-cat.js", "chat-cat.css", "chat-voice.js")))
    except OSError:
        return 0


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    """Render the web chat page."""
    workspace = _get_workspace(request)
    bot_name = _get_bot_name(workspace)
    profile_name = _get_active_profile_name(workspace)
    session_key = _webchat_session_key(getattr(request.app.state, "agent", None))
    loaded_at = time.time()  # before reading: a late reply saved after this reaches the tab on connect
    history = await asyncio.to_thread(_load_session_history, workspace, session_key)  # may copy files

    response = request.app.state.templates.TemplateResponse(request, "chat.html", {"bot_name": bot_name,
        "profile_name": profile_name,
        "session_key": session_key,
        "upload_max_files": _UPLOAD_MAX_FILES,
        "upload_max_bytes": _UPLOAD_MAX_BYTES,
        "cat_assets_version": _cat_assets_version(),
        # Voice in Web Chat: voice_recognition, voice_language, voice_groq_key
        **{f"voice_{name}": value for name, value in _voice_settings(request.app.state.config).items()},
        # The template inlines this JSON in a <script>: escape "<" so a message
        # containing "</script>" (e.g. an HTML code answer) cannot end it early.
        "history": json.dumps(history).replace("<", "\\u003c")})
    response.set_cookie(_LOADED_COOKIE, f"{loaded_at:.3f}", path="/chat", httponly=True, samesite="strict")
    return response


@router.post("/chat/new-conversation")
async def new_conversation(request: Request):
    """Start a fresh webchat session — mirrors /new command logic: consolidate then clear."""
    loop = request.app.state.agent
    if not loop:
        from fastapi.responses import JSONResponse
        return JSONResponse({"success": False, "error": "Agent not available"}, status_code=503)

    from fastapi.responses import JSONResponse
    session_key = _webchat_session_key(loop)
    # Wait for a running turn on this session (same lock as chat turns) instead of clearing under it
    async with loop._session_locks.setdefault(session_key, asyncio.Lock()):
        session = loop.sessions.get_or_create(session_key)

        # Snapshot unconsolidated messages before clearing
        snapshot = session.messages[session.last_consolidated:]
        session.clear()
        loop.sessions.save(session)
        loop.sessions.invalidate(session_key)

    # Archive snapshot to MEMORY.md in background (same as /new command)
    if snapshot:
        loop._schedule_background(loop.memory_consolidator.archive(snapshot))
        logger.info("[WebChat] New conversation: archiving {} messages to memory", len(snapshot))
    else:
        logger.info("[WebChat] New conversation: session was empty, nothing to archive")

    return JSONResponse({"success": True})


@router.post("/chat/upload")
async def upload_attachments(request: Request):
    """Save files attached in Web Chat; the chat message then lists them by id."""
    if not _same_origin(request.headers):
        return JSONResponse({"error": "Không nhận tệp gửi từ trang khác"}, status_code=403)
    if int(request.headers.get("content-length") or 0) > _UPLOAD_MAX_FILES * _UPLOAD_MAX_BYTES:
        return JSONResponse({"error": "Tệp gửi lên quá lớn"}, status_code=413)
    form = await request.form(max_files=_UPLOAD_MAX_FILES)
    try:
        uploads = [item for item in form.getlist("files") if isinstance(item, UploadFile)]
        if not uploads:
            return JSONResponse({"error": "Không có tệp nào"}, status_code=400)
        root = get_media_dir("webchat")
        folder = root / uuid.uuid4().hex[:12]
        folder.mkdir()
        saved = []
        try:
            for upload in uploads:
                path = _free_path(folder, _safe_filename(upload.filename))
                await _save_upload(upload, path)
                kind = "image" if _image_mime(path) else "file"
                saved.append({"id": path.relative_to(root).as_posix(), "size": path.stat().st_size,
                              **_attachment_info(path, kind)})
        except (OSError, ValueError) as e:
            shutil.rmtree(folder, ignore_errors=True)
            too_big = isinstance(e, ValueError)
            return JSONResponse({"error": str(e) if too_big else f"Không lưu được {upload.filename}: {e}"},
                                status_code=413 if too_big else 400)
    finally:
        await form.close()
    logger.info("[WebChat] Uploaded {} file(s) to {}", len(saved), folder.name)
    return JSONResponse({"files": saved})


@router.get("/chat/media/{rel:path}")
async def chat_media(rel: str):
    """Serve a stored attachment: images inline, any other file as a download."""
    path = _media_file(rel, get_media_dir())
    if path is None:
        return JSONResponse({"error": "Không tìm thấy tệp"}, status_code=404)
    headers = {"X-Content-Type-Options": "nosniff"}
    mime = _image_mime(path)
    if mime:
        return FileResponse(path, media_type=mime, headers=headers)
    return FileResponse(path, filename=path.name, headers=headers)  # attachment: never rendered here


# ── Voice (Web Chat) ────────────────────────────────────────
# The mic and read-aloud buttons are static/chat-voice.js. Speech is recognised by the browser itself
# or, when gateway.web.voice.recognition is "groq", by Groq's Whisper: the page records the mic and
# posts the audio to /chat/transcribe, gets the text back and sends it like a typed message. The
# recording is deleted as soon as it is transcribed; sessions only ever hold the text (custom).
_VOICE_TYPES = {  # what browsers record -> the file extension Groq tells the format by
    "audio/webm": ".webm", "video/webm": ".webm",  # Chrome, Brave, Edge
    "audio/ogg": ".ogg",  # Firefox
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "video/mp4": ".m4a",  # Safari
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mpeg": ".mp3", "audio/flac": ".flac",
}


def _voice_settings(config) -> dict:
    """What the chat page needs to know about voice: how speech is recognised, in which language,
    and whether Groq has a key (a flag: the key itself never reaches the page)."""
    voice = config.gateway.web.voice
    return {
        "recognition": "groq" if voice.recognition == "groq" else "browser",
        "language": voice.language or "vi-VN",
        "groq_key": bool(config.providers.groq.api_key or os.environ.get("GROQ_API_KEY")),
    }


def _groq_failure(error: TranscriptionError) -> str:
    """What the page says when Groq could not transcribe a recording, in place of "nothing heard"."""
    if is_certificate_error(error):  # trying again or another key would not help
        return ("Không kết nối an toàn được tới Groq vì chứng chỉ không xác minh được, thường do phần mềm "
                "diệt virus kiểm tra HTTPS (như Kaspersky). Hãy thêm api.groq.com vào danh sách loại trừ của nó")
    return f"Groq không chép lời được ({error}). Bấm Test ở mục Giọng nói trong Thiết lập để kiểm tra key và kết nối"


@router.post("/chat/transcribe")
async def transcribe_speech(request: Request):
    """Turn one recording from the Web Chat mic into text with Groq's Whisper."""
    if not _same_origin(request.headers):
        return JSONResponse({"error": "Không nhận ghi âm gửi từ trang khác"}, status_code=403)
    config = request.app.state.config
    voice = _voice_settings(config)
    if voice["recognition"] != "groq":
        return JSONResponse({"error": "Thiết lập đang nhận dạng giọng nói bằng trình duyệt, chưa chọn Groq"},
                            status_code=400)
    if not voice["groq_key"]:
        return JSONResponse({"error": "Thiếu key Groq: nhập ở Thiết lập → Accounts → Groq"}, status_code=400)
    # A recording is limited like one attachment (the page stops at 60 s, about 0.5 MB)
    too_big = JSONResponse({"error": f"Ghi âm quá {_UPLOAD_MAX_BYTES // (1024 * 1024)} MB"}, status_code=413)
    if int(request.headers.get("content-length") or 0) > 2 * _UPLOAD_MAX_BYTES:
        return too_big
    form = await request.form(max_files=1)
    try:
        upload = form.get("audio")
        if not isinstance(upload, UploadFile):
            return JSONResponse({"error": "Không có ghi âm"}, status_code=400)
        groq = config.providers.groq
        provider = GroqTranscriptionProvider(
            api_key=groq.api_key or None,
            api_base=groq.api_base,
            # transcription.model is the channels' setting: a Groq model only when they use Groq too
            model=config.transcription.model if config.get_transcription_provider_name() == "groq" else None,
            # No language: Whisper tells it from the speech. Told the setting's ("vi"), it wrote English
            # speech down in Vietnamese (user report 2026-10-04). The setting still serves the browser's
            # own recognition and the read-aloud voice.
            language=None,
        )
        suffix = _VOICE_TYPES.get((upload.content_type or "").split(";")[0].strip().lower(), ".webm")
        with tempfile.TemporaryDirectory(prefix="nanobot-voice-") as folder:  # deleted when the block ends
            path = Path(folder) / f"speech{suffix}"
            try:
                await _save_upload(upload, path)
            except ValueError:
                return too_big
            size = path.stat().st_size
            try:
                text = (await provider.transcribe(path, raise_errors=True)).strip()
            except TranscriptionError as e:  # the provider logged it; the page says why
                return JSONResponse({"error": _groq_failure(e)}, status_code=502)
    finally:
        await form.close()
    if text:
        logger.info("[WebChat] Speech transcribed: {} bytes -> {} characters", size, len(text))
    else:  # Groq answered, without text: nothing clear was said
        logger.warning("[WebChat] Speech gave no text ({} bytes)", size)
    return JSONResponse({"text": text})


def _same_origin(headers) -> bool:
    """False when a page from another site sent the request: its Origin is not this host."""
    origin = headers.get("origin")
    return not origin or urlsplit(origin).netloc.lower() == headers.get("host", "").lower()


def _socket_allowed(websocket: WebSocket) -> bool:
    """Apply the dashboard login to the chat socket.

    AuthMiddleware is a BaseHTTPMiddleware, which never sees WebSocket connections.
    Browsers also let any site open a socket to this port, so other origins are refused.
    """
    if not _same_origin(websocket.headers):
        return False
    if not getattr(websocket.app.state, "web_password", ""):
        return True
    return is_authenticated(websocket)


def _merge_window(websocket: WebSocket) -> float:
    """channels.debounce_seconds (0 = off), read per message so a change in Config applies at once."""
    try:
        return max(0.0, float(websocket.app.state.config.channels.debounce_seconds))
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _is_command(data: dict) -> bool:
    text = data.get("text")
    return isinstance(text, str) and text.lstrip().startswith("/")


_WEBCHAT_RECEIVED_AT = "_webchat_received_at"


def _mark_webchat_received(data: dict) -> None:
    """Attach a monotonic receive mark used only by internal latency telemetry."""
    if _WEBCHAT_RECEIVED_AT not in data:
        data[_WEBCHAT_RECEIVED_AT] = time.perf_counter()


def _webchat_timing_log(timing: dict, event: str, *, at: float | None = None, **fields: object) -> None:
    """Write latency telemetry without recording message content or user data."""
    now = time.perf_counter() if at is None else at
    elapsed_ms = (now - timing["received_at"]) * 1000
    details = "".join(f" {key}={value}" for key, value in fields.items())
    logger.info(
        "[WebChatTiming] turn={} event={} elapsed_ms={:.1f}{}",
        timing["turn_id"], event, elapsed_ms, details,
    )


def _take_batch(queued: list[dict], merge: bool) -> list[dict]:
    """Next turn's messages from the queue: the plain messages up to the next slash command
    (only the first one when merging is off), or one slash command on its own."""
    n = 1
    if merge and not _is_command(queued[0]):
        while n < len(queued) and not _is_command(queued[n]):
            n += 1
    batch = queued[:n]
    del queued[:n]
    return batch


async def _collect_burst(websocket: WebSocket, batch: list[dict], queued: list[dict], window: float) -> bool:
    """Like the channels' debounce: add messages sent less than `window` seconds apart to `batch`.

    A slash command ends the burst and waits in `queued` for a turn of its own.
    Returns True when the user pressed Stop meanwhile: the burst is dropped.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + window
    while (remaining := deadline - loop.time()) > 0:
        try:
            data = await asyncio.wait_for(websocket.receive_json(), remaining)
        except asyncio.TimeoutError:
            break
        _mark_webchat_received(data)
        if data.get("type") == "stop":
            return True
        if _is_command(data):
            queued.append(data)
            break
        batch.append(data)
        deadline = loop.time() + window
    return False


# ── Late replies ────────────────────────────────────────────
# A reply the gateway sends after the turn that asked for it (a sub-agent's result) goes out on the
# bus to channel "webchat", which is not a channel. The gateway's ChannelManager hands it to
# deliver_late_reply (registered in web/cli.py), which pushes it to every open Web Chat tab; a tab in
# the middle of a turn gets it once that turn is over, after its own answer.
_open_tabs: dict[WebSocket, dict] = {}  # socket -> {"workspace": Path, "busy": bool, "held": [events]}

# Late replies that found no open tab (the page closed, reloading or reconnecting) wait here; the next tab
# that connects gets the ones its page did not render. The page route stamps, in a cookie, when it read
# the history: a reply saved after that is not on the page. A notice (_notice) is never in the history.
_missed_replies: list[tuple[float, object]] = []  # (time.time() when it arrived, OutboundMessage)
_MISSED_MAX = 20
_MISSED_MAX_AGE_S = 24 * 3600
_LOADED_COOKIE = "nanobot_chat_loaded"


async def deliver_late_reply(msg) -> None:
    """ChannelManager sink for "webchat": push late replies to the open tabs."""
    if not (msg.metadata or {}).get("_late_reply"):
        return  # other "webchat" messages reach the page through the socket turn that sent them
    if not _open_tabs:
        _missed_replies.append((time.time(), msg))
        _prune_missed_replies()
        return
    workspace = next(iter(_open_tabs.values()))["workspace"]
    event = await _push_event(msg, workspace)
    for socket, tab in list(_open_tabs.items()):
        if tab["busy"]:
            tab["held"].append(event)
            continue
        try:
            await socket.send_json(event)
        except Exception:
            pass  # tab gone; the reply is saved in the session and shows after a reload


async def _push_event(msg, workspace: Path) -> dict:
    media = [await asyncio.to_thread(_share_file, path, workspace, refresh=True) for path in (msg.media or [])]
    return {"type": "push", "content": msg.content or "", "media": media}


def _prune_missed_replies() -> None:
    cutoff = time.time() - _MISSED_MAX_AGE_S
    _missed_replies[:] = [item for item in _missed_replies if item[0] >= cutoff][-_MISSED_MAX:]


async def _send_missed_replies(websocket: WebSocket, workspace: Path) -> None:
    """A tab connected: late replies that came while no tab was open, unless its page already shows
    them (saved before the page read the history)."""
    _prune_missed_replies()
    if not _missed_replies:
        return
    try:
        loaded_at = float(websocket.cookies.get(_LOADED_COOKIE) or 0)
    except ValueError:
        loaded_at = 0.0
    missed, _missed_replies[:] = list(_missed_replies), []
    for arrived_at, msg in missed:
        if arrived_at <= loaded_at and not (msg.metadata or {}).get("_notice"):
            continue  # in the history this page rendered
        try:
            await websocket.send_json(await _push_event(msg, workspace))
        except Exception:
            break


async def _release_held_replies(websocket: WebSocket) -> None:
    """The tab's turn is over: late replies that arrived meanwhile go out now."""
    tab = _open_tabs.get(websocket)
    if tab is None:
        return
    tab["busy"] = False
    held, tab["held"] = tab["held"], []
    for event in held:
        try:
            await websocket.send_json(event)
        except Exception:
            break


async def _stop_subagents(websocket: WebSocket, agent, session_key: str) -> None:
    """Stop also cancels the sub-agents this chat started, as /stop does on the channels. On a
    unified session that includes the ones started from other channels. A disconnect does not
    (user decision 2026-10-02) (custom)."""
    subagents = getattr(agent, "subagents", None)
    if subagents is None:
        return
    stopped = await subagents.cancel_by_session(session_key)
    if stopped:
        logger.info("[WebChat] Stop cancelled {} sub-agent(s)", stopped)
        try:
            await websocket.send_json({"type": "push", "content": f"⏹ Đã dừng {stopped} agent phụ.", "media": []})
        except Exception:
            pass


# ── Running sub-agents ──────────────────────────────────────
# Each tab watches this chat's sub-agents (AgentLoop.subagents) and gets them as a "subagents" event
# whenever they change. The page keeps Stop visible while there are any (Stop also cancels them,
# _stop_subagents above), and Chat focus shows their progress in a small panel (custom).
_SUBAGENT_TOOL_NAMES = {  # the tools a sub-agent can have (agent/subagent.py, _build_tools)
    "web_search": "tìm web",
    "web_fetch": "đọc trang web",
    "read_file": "đọc tệp",
    "write_file": "ghi tệp",
    "edit_file": "sửa tệp",
    "list_dir": "xem thư mục",
    "glob": "tìm tệp",
    "grep": "tìm trong tệp",
    "exec": "chạy lệnh",
}
_SUBAGENT_TOOL_HINTS = {  # a tool call in a sub-agent's box, followed by what it is about
    "web_search": "🔍 Tìm web",
    "web_fetch": "🌐 Đọc trang",
    "read_file": "📄 Đọc tệp",
    "write_file": "📝 Ghi tệp",
    "edit_file": "✏️ Sửa tệp",
    "list_dir": "📂 Xem thư mục",
    "glob": "🔎 Tìm tệp",
    "grep": "🔎 Tìm trong tệp",
    "exec": "⚙️ Chạy lệnh",
}
_SUBAGENT_PHASES = {  # SubagentStatus.phase; "awaiting_tools" names the tools instead
    "initializing": "Đang khởi động",
    "tools_completed": "Đang suy nghĩ",
    "final_response": "Đang viết kết quả",
    "done": "Xong",
    "error": "Lỗi",
}


def _subagent_line(entry: dict) -> dict:
    """One line of a sub-agent's box: what it said, a tool it called, or a tool that failed."""
    name = entry.get("name", "?")
    if entry.get("kind") == "tool":
        hint = _SUBAGENT_TOOL_HINTS.get(name, f"🛠 {name}")
        return {"kind": "tool", "text": f"{hint}: {entry['arg']}" if entry.get("arg") else hint}
    if entry.get("kind") == "error":
        label = _SUBAGENT_TOOL_HINTS.get(name, f"🛠 {name}").split(" ", 1)[-1]  # the hint without its icon
        return {"kind": "error", "text": f"✗ {label} lỗi: {entry.get('text', '')}"[:200]}
    return {"kind": "thought", "text": str(entry.get("text", ""))[:300]}


def _subagent_items(agent, session_key: str) -> list[dict]:
    """This chat's running sub-agents in the order they started, as the page shows them."""
    statuses = getattr(getattr(agent, "subagents", None), "statuses_for_session", None)
    if not callable(statuses):
        return []
    now = time.monotonic()
    items = []
    for task_id, st in statuses(session_key).items():
        running = list(dict.fromkeys(_SUBAGENT_TOOL_NAMES.get(t, t) for t in getattr(st, "current_tools", [])))
        doing = f"Đang {', '.join(running)}" if st.phase == "awaiting_tools" and running \
            else _SUBAGENT_PHASES.get(st.phase, "Đang làm")
        items.append({
            "id": task_id,
            "label": st.label,
            "task": st.task_description[:300],
            "doing": doing,
            "round": st.iteration + 1,
            "lines": [_subagent_line(entry) for entry in getattr(st, "progress", [])],  # the last 12; the box scrolls
            "elapsed": int(now - st.started_at),
        })
    return items


async def _watch_subagents(websocket: WebSocket, agent, session_key: str) -> None:
    """Send the tab this chat's running sub-agents when they change, checking every second. Nothing is
    sent before the first one starts; the page counts the elapsed time itself."""
    last: list[dict] = []
    while True:
        try:
            items = _subagent_items(agent, session_key)
            shown = [{k: v for k, v in item.items() if k != "elapsed"} for item in items]
            if shown != last:
                last = shown
                await websocket.send_json({"type": "subagents", "items": items})
        except Exception:
            return  # the tab is gone
        await asyncio.sleep(1)


@router.websocket("/chat/ws")
async def chat_websocket(websocket: WebSocket):
    """Handle WebSocket chat connection."""
    if not _socket_allowed(websocket):
        # debug: an open chat page retries every 3 s, e.g. after a restart drops its login
        logger.debug("[WebChat] Refused socket: not logged in, or opened from another site")
        await websocket.close(code=1008)
        return
    await websocket.accept()
    agent = websocket.app.state.agent
    session_key = _webchat_session_key(agent)
    workspace = _get_workspace(websocket)  # the message tool resolves relative paths against it

    if not agent:
        await websocket.send_json({"type": "error", "content": "Agent not available"})
        await websocket.close()
        return

    logger.info("[WebChat] Client connected")
    _open_tabs[websocket] = {"workspace": workspace, "busy": False, "held": []}
    await _send_missed_replies(websocket, workspace)
    watcher = asyncio.create_task(_watch_subagents(websocket, agent, session_key))

    try:
        processing_task: asyncio.Task | None = None
        # Messages sent while a turn was running. With channels.debounce_seconds on, the next turn
        # answers them together, as Telegram and Zalo merge quick messages; a slash command still
        # gets a turn of its own. With it off, they are answered one by one.
        queued: list[dict] = []

        while True:
            window = _merge_window(websocket)
            if queued:
                batch = _take_batch(queued, merge=window > 0)
            else:
                data = await websocket.receive_json()
                _mark_webchat_received(data)
                if data.get("type") == "stop":
                    await _stop_subagents(websocket, agent, session_key)  # no turn is running
                    continue
                batch = [data]
                # Wait briefly for follow-up messages so a quick series becomes one turn
                if window > 0 and not _is_command(data) and await _collect_burst(websocket, batch, queued, window):
                    logger.info("[WebChat] User requested stop before the turn started")
                    await _stop_subagents(websocket, agent, session_key)
                    continue

            texts: list[str] = []
            files: list[Path] = []
            for data in batch:
                # Attachments: ids returned by /chat/upload, checked message by message
                ids = data.get("media") if isinstance(data.get("media"), list) else []
                found = [_media_file(i, get_media_dir("webchat")) if isinstance(i, str) else None for i in ids]
                if None in found or len(found) > _UPLOAD_MAX_FILES:
                    await websocket.send_json({"type": "error", "content":
                        f"Không gửi được: tệp đính kèm không còn trên máy chủ hoặc quá {_UPLOAD_MAX_FILES} tệp."
                        " Hãy đính kèm lại."})
                    continue
                text = data.get("text", "").strip()
                if text:
                    texts.append(text)
                files += found

            user_message = "\n".join(texts)
            if not user_message and not files:
                continue

            received_marks = [
                data.get(_WEBCHAT_RECEIVED_AT)
                for data in batch
                if isinstance(data.get(_WEBCHAT_RECEIVED_AT), (int, float))
            ]
            timing = {
                "turn_id": uuid.uuid4().hex[:12],
                "received_at": min(received_marks, default=time.perf_counter()),
            }
            _webchat_timing_log(
                timing,
                "debounce_complete",
                batch_size=len(batch),
                debounce_ms=f"{(time.perf_counter() - timing['received_at']) * 1000:.1f}",
            )

            logger.info("[WebChat] User: {}{}{}", user_message[:100], f" (+{len(files)} file(s))" if files else "",
                        f" ({len(batch)} messages merged)" if len(batch) > 1 else "")

            # Typing indicator; "upto" is the last message (the page numbers them "seq") this turn answers
            seqs = [d["seq"] for d in batch if isinstance(d.get("seq"), int)]
            await websocket.send_json({"type": "typing", "content": "", "upto": max(seqs, default=None)})

            # Stream progress callback
            async def on_progress(content: str, **kwargs):
                try:
                    if kwargs.get("tool_hint"):
                        content = _friendly_tool_hint(content)
                    await websocket.send_json({"type": "progress", "content": content})
                except Exception:
                    pass

            # Real streaming callbacks — send token deltas to browser
            streamed = False
            stream_buf = []
            last_segment = ""
            first_delta_received = False
            first_delta_sent = False

            async def on_stream(delta: str):
                nonlocal streamed, first_delta_received, first_delta_sent
                streamed = True
                stream_buf.append(delta)
                if delta and not first_delta_received:
                    first_delta_received = True
                    _webchat_timing_log(timing, "first_delta_received")
                try:
                    await websocket.send_json({"type": "stream_delta", "content": delta})
                    if delta and not first_delta_sent:
                        first_delta_sent = True
                        _webchat_timing_log(timing, "first_delta_sent")
                except Exception:
                    if delta and not first_delta_sent:
                        _webchat_timing_log(timing, "first_delta_send_failed")
                    pass

            async def on_stream_end(*, resuming: bool = False, **kwargs):
                # Called once per streamed segment; resuming=True means tool calls follow and the
                # turn goes on. Send only this segment so the next bubble does not repeat it.
                nonlocal last_segment
                segment = "".join(stream_buf)
                stream_buf.clear()
                if segment:
                    last_segment = segment
                try:
                    await websocket.send_json({"type": "stream_end", "content": segment, "resuming": resuming})
                except Exception:
                    pass

            async def _do_process():
                process_started_at = time.perf_counter()
                _webchat_timing_log(timing, "processing_start", at=process_started_at)
                # Reading documents can take a while (big PDFs): keep it off the event loop.
                content, images = await asyncio.to_thread(_with_attachments, user_message, files)
                _webchat_timing_log(timing, "attachments_ready")
                # Same per-session lock as channel messages (AgentLoop._dispatch): two tabs, a reload
                # mid-answer, or Zalo on a unified session must not run two turns on one session.
                async with agent._session_locks.setdefault(session_key, asyncio.Lock()):
                    _webchat_timing_log(timing, "session_lock_acquired")
                    turn_started = datetime.now().isoformat()
                    process_kwargs = {
                        "content": content,
                        "session_key": session_key,
                        "channel": "webchat",
                        "chat_id": "web_user",
                        "media": images or None,
                        "on_progress": on_progress,
                        "on_stream": on_stream,
                        "on_stream_end": on_stream_end,
                    }
                    # AgentLoop carries this opaque id through the runner so provider timing
                    # records can be correlated without putting telemetry in user messages.
                    if hasattr(agent, "_run_agent_loop"):
                        process_kwargs["metadata"] = {"_webchat_timing_id": timing["turn_id"]}
                    resp = await agent.process_direct(**process_kwargs)
                    _webchat_timing_log(timing, "agent_returned")
                    # Messages this turn saved (stamped when saved), for the files it sent
                    turn = [m for m in agent.sessions.get_or_create(session_key).messages
                            if str(m.get("timestamp", "")) >= turn_started]
                sent = await asyncio.to_thread(
                    lambda: [b for m in turn for b in _sent_files(m, workspace, refresh=True)])
                response = resp.content if resp else ""
                # The bot sent its reply with files (message tool): their bubble below is the answer, so the
                # same text does not come again as a final message (custom, answer with files)
                answered_with_files = bool(sent) and bool((getattr(resp, "metadata", None) or {}).get("_answered_with_files"))
                # Send the final text unless it was the last streamed segment: an error or a
                # tool-only ending can follow an earlier segment without being streamed itself.
                if not answered_with_files and (
                        not streamed or (response and strip_think(response) != strip_think(last_segment))):
                    try:
                        await websocket.send_json({"type": "message", "content": response})
                    except Exception:
                        pass  # browser gone (e.g. reloaded); the answer is saved in the session
                for bubble in sent:  # files the agent sent with the message tool, after its reply
                    try:
                        await websocket.send_json({"type": "push", "content": bubble["content"],
                                                   "media": bubble["media"]})
                    except Exception:
                        pass
                logger.info("[WebChat] Bot: {}{}", response[:100] if response else "(empty)",
                            f" (+{len(sent)} file message(s))" if sent else "")
                _webchat_timing_log(timing, "turn_complete")

            _open_tabs[websocket]["busy"] = True  # late replies wait for this turn's own answer
            processing_task = asyncio.create_task(_do_process())

            # Wait for task while still accepting stop signals
            while not processing_task.done():
                # Listen for both: task completion and new WebSocket messages
                receive_task = asyncio.create_task(websocket.receive_json())
                done, _ = await asyncio.wait(
                    {processing_task, receive_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if receive_task in done:
                    msg = receive_task.result()
                    _mark_webchat_received(msg)
                    if msg.get("type") == "stop":
                        queued.clear()
                        processing_task.cancel()
                        logger.info("[WebChat] User requested stop — cancelling task")
                        try:
                            await processing_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        await _stop_subagents(websocket, agent, session_key)
                        break
                    if msg.get("text", "").strip() or msg.get("media"):
                        queued.append(msg)  # answered after this turn (merged), not dropped
                else:
                    receive_task.cancel()
                    try:
                        await receive_task
                    except (asyncio.CancelledError, Exception):
                        pass

            # Ensure task exceptions don't go unhandled
            if processing_task.done() and not processing_task.cancelled():
                try:
                    processing_task.result()
                except Exception as e:
                    logger.error("[WebChat] Error processing message: {}", e)
                    try:
                        await websocket.send_json({
                            "type": "error",
                            "content": f"Error: {str(e)}"
                        })
                    except Exception:
                        pass
            await _release_held_replies(websocket)
    except (WebSocketDisconnect, ConnectionResetError):
        logger.info("[WebChat] Client disconnected")
    except asyncio.CancelledError:
        logger.info("[WebChat] Connection cancelled (server shutdown)")
    except Exception as e:
        logger.error("[WebChat] WebSocket error: {}", e)
    finally:
        _open_tabs.pop(websocket, None)
        watcher.cancel()
        try:
            await websocket.close()
        except Exception:
            pass
