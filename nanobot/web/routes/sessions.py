"""Sessions viewer routes — list sessions and view chat history."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from loguru import logger

router = APIRouter()


@router.get("/sessions", response_class=HTMLResponse)
async def sessions_list(request: Request):
    """Render the sessions list page."""
    app_state = request.app.state
    session_manager = app_state.session_manager

    sessions = []
    if session_manager:
        raw_sessions = session_manager.list_sessions()
        for s in raw_sessions:
            # Count messages by loading session
            key = s.get("key", "")
            try:
                session = session_manager.get_or_create(key)
                msg_count = len(session.messages)
            except Exception:
                msg_count = 0

            sessions.append({
                "key": key,
                "created_at": _format_datetime(s.get("created_at")),
                "updated_at": _format_datetime(s.get("updated_at")),
                "message_count": msg_count,
            })

    context = {
        "request": request,
        "sessions": sessions,
        "total_count": len(sessions),
    }

    return app_state.templates.TemplateResponse(request, "sessions.html", context)


@router.get("/sessions/{key:path}", response_class=HTMLResponse)
async def session_detail(request: Request, key: str):
    """Render a single session's chat history."""
    app_state = request.app.state
    session_manager = app_state.session_manager

    messages = []
    if session_manager:
        try:
            session = session_manager.get_or_create(key)
            for msg in session.messages:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                timestamp = msg.get("timestamp", "")

                # Handle tool_calls in assistant messages
                tool_calls = msg.get("tool_calls")
                tool_call_id = msg.get("tool_call_id")

                messages.append({
                    "role": role,
                    "content": content,
                    "timestamp": _format_datetime(timestamp),
                    "has_tool_calls": bool(tool_calls),
                    "tool_call_id": tool_call_id,
                    "tool_calls": tool_calls,
                })
        except Exception as e:
            logger.error(f"[Web Sessions] Failed to load session {key}: {e}")

    context = {
        "request": request,
        "session_key": key,
        "messages": messages,
        "message_count": len(messages),
    }

    return app_state.templates.TemplateResponse(request, "session_detail.html", context)


def _format_datetime(dt_str: str | None) -> str:
    """Format an ISO datetime string to a readable format."""
    if not dt_str:
        return "—"
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return dt_str[:16] if len(dt_str) > 16 else dt_str
