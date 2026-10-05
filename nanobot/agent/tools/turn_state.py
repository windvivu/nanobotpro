"""Per-turn routing state for the tools every session shares (custom, not in upstream).

AgentLoop keeps one instance of each tool for all sessions, and several turns run at once: up to
NANOBOT_MAX_CONCURRENT_REQUESTS from the bus, plus Web Chat and cron. The chat a `message` goes
to, the chat a sub-agent reports back to, the "already sent in this turn" flag and the like used
to sit on the tool instance, so one session could overwrite another's while a tool was waiting.
Upstream solved it with RequestContext; this is the small local version.

begin_turn() gives the current asyncio task a fresh state; AgentLoop._process_message calls it
first. A TurnLocal field then reads and writes that state. Tools that run in child tasks
(concurrent tool calls) share their turn's state, so a flag a tool sets is seen by its turn.
Outside a turn (construction, tests) a field is a plain per-instance value, as before.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

_turn: ContextVar[dict[tuple[int, str], Any] | None] = ContextVar("nanobot_turn_state", default=None)


def begin_turn() -> None:
    """Start one turn in the current task. Its values stay readable once the turn is over: the
    cron callback checks the message tool's _sent_in_turn after process_direct returns."""
    _turn.set({})


class TurnLocal:
    """A tool field kept per turn. Its first assignment, normally in __init__, is the value every
    turn starts from."""

    def __set_name__(self, owner: type, name: str) -> None:
        self._name = name
        self._default = f"_turn_default{name}"

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        state = _turn.get()
        if state is not None and (key := (id(obj), self._name)) in state:
            return state[key]
        try:
            return obj.__dict__[self._default]
        except KeyError:
            raise AttributeError(self._name) from None

    def __set__(self, obj: Any, value: Any) -> None:
        state = _turn.get()
        if state is None or self._default not in obj.__dict__:
            obj.__dict__[self._default] = value
        if state is not None:
            state[(id(obj), self._name)] = value
