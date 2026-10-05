"""Sustained goal tools backed by session metadata."""

from __future__ import annotations

from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.agent.tools.turn_state import TurnLocal
from nanobot.session.goal_state import (
    ACTIVE_STATUS,
    make_active_goal_state,
)
from nanobot.session.manager import SessionManager


def _session_key_from_context(
    session_key: str | None,
    channel: str | None,
    chat_id: str | None,
) -> str | None:
    if session_key:
        return session_key
    if channel and chat_id:
        return f"{channel}:{chat_id}"
    return None


class _GoalContextMixin:
    # One instance serves every session: the session a goal belongs to is per turn (custom)
    _channel = TurnLocal()
    _chat_id = TurnLocal()
    _session_key = TurnLocal()

    def set_context(
        self,
        channel: str,
        chat_id: str,
        session_key: str | None = None,
    ) -> None:
        """Set current message/session routing context."""
        self._channel = channel
        self._chat_id = chat_id
        self._session_key = session_key

    def _resolve_session_key(self) -> str | None:
        return _session_key_from_context(self._session_key, self._channel, self._chat_id)


@tool_parameters(
    tool_parameters_schema(
        objective=StringSchema("The sustained objective to remember across future turns"),
        ui_summary=StringSchema("Optional short summary for display and runtime context"),
        required=["objective"],
    )
)
class LongTaskTool(_GoalContextMixin, Tool):
    """Tool to start a sustained goal for the current session."""

    def __init__(self, sessions: SessionManager):
        self._sessions = sessions
        self._channel = None
        self._chat_id = None
        self._session_key = None

    @property
    def name(self) -> str:
        return "long_task"

    @property
    def description(self) -> str:
        return (
            "Start a sustained goal for this conversation. Use when the user asks "
            "you to keep working toward an objective across turns or after this "
            "response. Refuse to replace an active goal; complete it first."
        )

    async def execute(
        self,
        objective: str,
        ui_summary: str | None = None,
        **_kwargs: Any,
    ) -> str:
        session_key = self._resolve_session_key()
        if not session_key:
            return "Error: No active session context for long_task."

        objective = (objective or "").strip()
        if not objective:
            return "Error: objective is required."

        current = self._sessions.get_goal_state(session_key)
        if current and current.get("status") == ACTIVE_STATUS:
            return "Error: An active goal already exists. Use complete_goal before starting a new long_task."

        state = make_active_goal_state(objective, ui_summary)
        self._sessions.set_goal_state(session_key, state)
        return "Long task goal saved for this session."


@tool_parameters(
    tool_parameters_schema(
        recap=StringSchema("Optional recap of what was completed"),
    )
)
class CompleteGoalTool(_GoalContextMixin, Tool):
    """Tool to complete the active sustained goal for the current session."""

    def __init__(self, sessions: SessionManager):
        self._sessions = sessions
        self._channel = None
        self._chat_id = None
        self._session_key = None

    @property
    def name(self) -> str:
        return "complete_goal"

    @property
    def description(self) -> str:
        return (
            "Mark the current sustained goal as completed for this conversation. "
            "Use after the active long_task objective has been fulfilled."
        )

    async def execute(self, recap: str | None = None, **_kwargs: Any) -> str:
        session_key = self._resolve_session_key()
        if not session_key:
            return "Error: No active session context for complete_goal."

        completed = self._sessions.complete_goal_state(session_key, recap)
        if not completed:
            return "No active goal to complete."
        return "Active goal marked completed."

