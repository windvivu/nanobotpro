"""Web Chat — real-time chat via WebSocket."""

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from loguru import logger

import re

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
                content = msg.get("content", "")
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})
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


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    """Render the web chat page."""
    workspace = _get_workspace(request)
    bot_name = _get_bot_name(workspace)
    profile_name = _get_active_profile_name(workspace)
    session_key = _webchat_session_key(getattr(request.app.state, "agent", None))
    history = _load_session_history(workspace, session_key)

    return request.app.state.templates.TemplateResponse(request, "chat.html", {"bot_name": bot_name,
        "profile_name": profile_name,
        "session_key": session_key,
        "history": json.dumps(history)})


@router.post("/chat/new-conversation")
async def new_conversation(request: Request):
    """Start a fresh webchat session — mirrors /new command logic: consolidate then clear."""
    loop = request.app.state.agent
    if not loop:
        from fastapi.responses import JSONResponse
        return JSONResponse({"success": False, "error": "Agent not available"}, status_code=503)

    from fastapi.responses import JSONResponse
    session_key = _webchat_session_key(loop)
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




@router.websocket("/chat/ws")
async def chat_websocket(websocket: WebSocket):
    """Handle WebSocket chat connection."""
    await websocket.accept()
    agent = websocket.app.state.agent
    session_key = _webchat_session_key(agent)

    if not agent:
        await websocket.send_json({"type": "error", "content": "Agent not available"})
        await websocket.close()
        return

    logger.info("[WebChat] Client connected")

    try:
        processing_task: asyncio.Task | None = None

        while True:
            data = await websocket.receive_json()

            # Handle stop signal — cancel running task
            if data.get("type") == "stop":
                if processing_task and not processing_task.done():
                    processing_task.cancel()
                    logger.info("[WebChat] User requested stop — cancelling task")
                continue

            user_message = data.get("text", "").strip()
            if not user_message:
                continue

            logger.info("[WebChat] User: {}", user_message[:100])

            # Send typing indicator
            await websocket.send_json({"type": "typing", "content": ""})

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

            async def on_stream(delta: str):
                nonlocal streamed
                streamed = True
                stream_buf.append(delta)
                try:
                    await websocket.send_json({"type": "stream_delta", "content": delta})
                except Exception:
                    pass

            async def on_stream_end(**kwargs):
                try:
                    full_text = "".join(stream_buf)
                    await websocket.send_json({"type": "stream_end", "content": full_text})
                except Exception:
                    pass

            async def _do_process():
                resp = await agent.process_direct(
                    content=user_message,
                    session_key=session_key,
                    channel="webchat",
                    chat_id="web_user",
                    on_progress=on_progress,
                    on_stream=on_stream,
                    on_stream_end=on_stream_end,
                )
                response = resp.content if resp else ""
                if not streamed:
                    await websocket.send_json({"type": "message", "content": response})
                logger.info("[WebChat] Bot: {}", response[:100] if response else "(empty)")

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
                    if msg.get("type") == "stop":
                        processing_task.cancel()
                        logger.info("[WebChat] User requested stop — cancelling task")
                        try:
                            await processing_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        break
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
    except (WebSocketDisconnect, ConnectionResetError):
        logger.info("[WebChat] Client disconnected")
    except asyncio.CancelledError:
        logger.info("[WebChat] Connection cancelled (server shutdown)")
    except Exception as e:
        logger.error("[WebChat] WebSocket error: {}", e)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
