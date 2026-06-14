"""Helpers for sustained goal state stored in session metadata."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

GOAL_STATE_KEY = "goal_state"
LEGACY_GOAL_STATE_KEY = "thread_goal"

ACTIVE_STATUS = "active"
COMPLETED_STATUS = "completed"

MAX_OBJECTIVE_RUNTIME_CHARS = 4000
MAX_SUMMARY_RUNTIME_CHARS = 120


def utc_now_iso() -> str:
    """Return an ISO timestamp with UTC timezone."""
    return datetime.now(timezone.utc).isoformat()


def goal_state_raw(metadata: Mapping[str, Any] | None) -> Any:
    """Return current or legacy goal state blob from metadata."""
    if not metadata:
        return None
    if GOAL_STATE_KEY in metadata:
        return metadata.get(GOAL_STATE_KEY)
    return metadata.get(LEGACY_GOAL_STATE_KEY)


def parse_goal_state(blob: Any) -> dict[str, Any] | None:
    """Return a normalized goal state dict, or None for invalid values."""
    if not isinstance(blob, Mapping):
        return None
    status = blob.get("status")
    objective = str(blob.get("objective") or "").strip()
    if status not in {ACTIVE_STATUS, COMPLETED_STATUS} or not objective:
        return None
    parsed = dict(blob)
    parsed["status"] = status
    parsed["objective"] = objective
    return parsed


def current_goal_state(metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return parsed goal state from metadata, accepting the legacy key."""
    return parse_goal_state(goal_state_raw(metadata))


def sustained_goal_active(metadata: Mapping[str, Any] | None) -> bool:
    """Whether metadata has an active sustained goal."""
    state = current_goal_state(metadata)
    return bool(state and state.get("status") == ACTIVE_STATUS)


def discard_legacy_goal_state_key(metadata: dict[str, Any]) -> None:
    """Remove the legacy goal key after a write migrates to goal_state."""
    metadata.pop(LEGACY_GOAL_STATE_KEY, None)


def make_active_goal_state(objective: str, ui_summary: str | None = None) -> dict[str, Any]:
    """Build a new active goal state blob."""
    state: dict[str, Any] = {
        "status": ACTIVE_STATUS,
        "objective": objective.strip(),
        "started_at": utc_now_iso(),
    }
    summary = (ui_summary or "").strip()
    if summary:
        state["ui_summary"] = summary[:MAX_SUMMARY_RUNTIME_CHARS]
    return state


def make_completed_goal_state(state: Mapping[str, Any], recap: str | None = None) -> dict[str, Any]:
    """Return a completed copy of an active goal state."""
    completed = dict(state)
    completed["status"] = COMPLETED_STATUS
    completed["completed_at"] = utc_now_iso()
    if recap is not None:
        completed["recap"] = recap.strip()
    return completed


def goal_state_runtime_lines(metadata: Mapping[str, Any] | None) -> list[str]:
    """Return untrusted runtime metadata lines for an active goal."""
    state = current_goal_state(metadata)
    if not state or state.get("status") != ACTIVE_STATUS:
        return []

    objective = state["objective"][:MAX_OBJECTIVE_RUNTIME_CHARS]
    lines = ["", "Goal (active):", objective]
    summary = str(state.get("ui_summary") or "").strip()
    if summary:
        lines.append(f"Summary: {summary[:MAX_SUMMARY_RUNTIME_CHARS]}")
    return lines

