"""Presentation helpers for Adminbot web templates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from adminbot.app.registry import BotRecord


@dataclass(frozen=True, slots=True)
class BotSummary:
    id: str
    name: str
    description: str
    status: str
    workspace: str
    config_path: str
    web_port: int
    pid: str
    last_run_at: str
    updated_at: str
    dashboard_url: str
    last_stopped_at: str
    exit_code: str
    status_tone: str
    attention: bool
    status_detail: str
    updated_label: str
    last_run_label: str
    last_stopped_label: str


def _parse_iso(value: str | None) -> datetime | None:
    if not value or value == "-":
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_relative(value: str | None) -> str:
    dt = _parse_iso(value)
    if not dt:
        return "-"

    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt.astimezone(timezone.utc)
    seconds = int(max(delta.total_seconds(), 0))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes}m ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours}h ago"
    days = seconds // 86400
    return f"{days}d ago"


def _format_timestamp(value: str | None) -> str:
    dt = _parse_iso(value)
    if not dt:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _dashboard_url(bot: BotRecord, request_base_url: str | None = None) -> str:
    if request_base_url:
        parsed = urlsplit(request_base_url)
        scheme = parsed.scheme or "http"
        host = parsed.hostname or "127.0.0.1"
        return f"{scheme}://{host}:{bot.web_port}"
    return f"http://127.0.0.1:{bot.web_port}"


def build_bot_summary(bot: BotRecord, request_base_url: str | None = None) -> BotSummary:
    is_running = bot.process.status == "running"
    exit_code = bot.process.exit_code
    last_stopped_at = bot.process.last_stopped_at or "-"
    attention = not is_running and (exit_code not in (None, 0))
    if is_running:
        status_detail = "Process is active and responding to status checks."
        status_tone = "running"
    elif exit_code not in (None, 0):
        status_detail = f"Stopped after a non-zero exit ({exit_code})."
        status_tone = "warning"
    elif bot.last_run_at:
        status_detail = "Stopped cleanly after the last known run."
        status_tone = "stopped"
    else:
        status_detail = "Registered but has not been started yet."
        status_tone = "idle"

    return BotSummary(
        id=bot.id,
        name=bot.name,
        description=bot.description,
        status=bot.process.status,
        workspace=bot.workspace,
        config_path=bot.config_path,
        web_port=bot.web_port,
        pid=str(bot.process.pid) if bot.process.pid else "-",
        last_run_at=_format_timestamp(bot.last_run_at),
        updated_at=_format_timestamp(bot.updated_at),
        dashboard_url=_dashboard_url(bot, request_base_url),
        last_stopped_at=_format_timestamp(last_stopped_at),
        exit_code=str(exit_code) if exit_code is not None else "-",
        status_tone=status_tone,
        attention=attention,
        status_detail=status_detail,
        updated_label=_format_relative(bot.updated_at),
        last_run_label=_format_relative(bot.last_run_at),
        last_stopped_label=_format_relative(last_stopped_at),
    )
