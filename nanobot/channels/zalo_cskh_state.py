"""Workspace-scoped CSKH state. No bridge, bus, or conversation text is stored here."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Literal
from zoneinfo import ZoneInfo

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field


class _Customer(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = ""
    first_seen: float
    last_seen: float
    messages_in: int = Field(default=0, ge=0)
    messages_out: int = Field(default=0, ge=0)
    limited_count: int = Field(default=0, ge=0)
    status: Literal["active", "paused", "blocked"] = "active"
    paused_until: float | None = None
    escalations: list[dict] = Field(default_factory=list)
    inbound_day: str = ""
    inbound_today: int = Field(default=0, ge=0)
    day_notice_sent: bool = False


class _Daily(BaseModel):
    new_customers: int = Field(default=0, ge=0)
    inbound: int = Field(default=0, ge=0, alias="in")
    outbound: int = Field(default=0, ge=0, alias="out")
    limited: int = Field(default=0, ge=0)
    escalations: int = Field(default=0, ge=0)
    friend_accepts: int = Field(default=0, ge=0)
    blocked_dropped: int = Field(default=0, ge=0)


class _State(BaseModel):
    customers: dict[str, _Customer] = Field(default_factory=dict)
    daily: dict[str, _Daily] = Field(default_factory=dict)
    paused_all: bool = False


class CustomerServiceState:
    """One store per channel/workspace; writes are locked and replaced atomically.

    Times are Unix seconds; day keys use the host's local timezone by default.
    Use snapshot()/mutators instead of changing the in-memory dictionaries.
    """

    def __init__(self, workspace: Path, *, timezone: str | None = None,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic):
        self.path = Path(workspace) / "zalo_cskh" / "state.json"
        self._timezone = ZoneInfo(timezone) if timezone else None
        self._clock = clock
        self._monotonic = monotonic
        self._minute_timestamps: dict[str, deque[float]] = {}
        self._minute_notice_until: dict[str, float] = {}
        self._lock = threading.RLock()
        self._data = _State().model_dump(by_alias=True)
        if self.path.exists():
            try:
                data = _State.model_validate_json(self.path.read_text(encoding="utf-8"))
                for day in data.daily:
                    date.fromisoformat(day)
                self._data = data.model_dump(by_alias=True)
            except (ValueError, OSError) as exc:
                logger.warning("Zalo customerService state could not be loaded; starting empty: {}", exc)

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def _day(self, now: float) -> str:
        return datetime.fromtimestamp(now, self._timezone).date().isoformat()

    def _daily(self, now: float) -> dict:
        return self._data["daily"].setdefault(self._day(now), _Daily().model_dump(by_alias=True))

    def _customer(self, sender_id: str, name: str, now: float) -> dict:
        customer = self._data["customers"].get(sender_id)
        if customer is None:
            customer = _Customer(name=name, first_seen=now, last_seen=now).model_dump()
            self._data["customers"][sender_id] = customer
            self._daily(now)["new_customers"] += 1
        if name:
            customer["name"] = name
        customer["last_seen"] = now
        return customer

    def _prune(self, now: float) -> None:
        oldest_day = datetime.fromtimestamp(now, self._timezone).date() - timedelta(days=89)
        self._data["daily"] = {key: value for key, value in self._data["daily"].items()
                               if date.fromisoformat(key) >= oldest_day}
        self._data["customers"] = {key: value for key, value in self._data["customers"].items()
                                   if value["last_seen"] >= now - 180 * 86400}
        tick = self._monotonic()
        self._minute_timestamps = {key: value for key, value in self._minute_timestamps.items()
                                   if value and value[-1] > tick - 60}
        self._minute_notice_until = {key: value for key, value in self._minute_notice_until.items()
                                     if value > tick}

    def _save(self, now: float) -> None:
        self._prune(now)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".state-", suffix=".tmp", delete=False) as stream:
                temp_path = Path(stream.name)
                json.dump(self._data, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _update(self, change: Callable[[float], object]):
        with self._lock:
            before = copy.deepcopy(self._data)
            before_minute = copy.deepcopy(self._minute_timestamps)
            before_notices = self._minute_notice_until.copy()
            now = self._clock()
            try:
                result = change(now)
                self._save(now)
                return result
            except Exception:
                self._data = before
                self._minute_timestamps = before_minute
                self._minute_notice_until = before_notices
                raise

    def admit_inbound(self, sender_id: str, name: str, *, per_minute: int, per_day: int) -> tuple[bool, str | None]:
        """Persist one receive decision before publishing to the bus.

        Returns (allowed, notice_kind). Groups must be dropped by the caller.
        All non-paused/non-blocked attempts count toward quotas, including spam.
        Notice flags reserve one attempt; a failed send must not trigger a flood.
        """
        def change(now):
            status = self.customer_status(sender_id)
            daily = self._daily(now)
            if status == "blocked":
                daily["blocked_dropped"] += 1
                # Recent activity keeps a blocked customer from expiring silently.
                self._data["customers"][sender_id]["last_seen"] = now
                return False, None
            customer = self._customer(sender_id, name, now)
            customer["messages_in"] += 1
            daily["in"] += 1
            if status == "paused":
                return False, None
            if customer["status"] == "paused":
                customer["status"] = "active"
                customer["paused_until"] = None

            today = self._day(now)
            if customer["inbound_day"] != today:
                customer.update(inbound_day=today, inbound_today=0, day_notice_sent=False)
            customer["inbound_today"] += 1
            tick = self._monotonic()
            recent = self._minute_timestamps.setdefault(sender_id, deque())
            while recent and recent[0] <= tick - 60:
                recent.popleft()
            recent.append(tick)

            notice = None
            if len(recent) > per_minute:
                if tick >= self._minute_notice_until.get(sender_id, float("-inf")):
                    self._minute_notice_until[sender_id] = tick + 60
                    notice = "minute"
            elif customer["inbound_today"] > per_day:
                if not customer["day_notice_sent"]:
                    customer["day_notice_sent"] = True
                    notice = "day"
            else:
                return True, None
            customer["limited_count"] += 1
            daily["limited"] += 1
            return False, notice

        return self._update(change)

    def customer_status(self, sender_id: str) -> str:
        with self._lock:
            customer = self._data["customers"].get(sender_id, {})
            status = customer.get("status", "active")
            if status == "blocked":
                return "blocked"
            if self._data["paused_all"]:
                return "paused"
            until = customer.get("paused_until")
            if status == "paused" and (until is None or until > self._clock()):
                return "paused"
            return "active"

    def forget_customer(self, sender_id: str) -> None:
        """Remove one customer's state and quota caches through the shared store."""
        def change(now):
            self._data["customers"].pop(sender_id, None)
            self._minute_timestamps.pop(sender_id, None)
            self._minute_notice_until.pop(sender_id, None)
        self._update(change)

    def record_inbound(self, sender_id: str, name: str = "") -> None:
        def change(now):
            customer = self._customer(sender_id, name, now)
            customer["messages_in"] += 1
            self._daily(now)["in"] += 1
        self._update(change)

    def record_outbound(self, sender_id: str) -> None:
        def change(now):
            self._customer(sender_id, "", now)["messages_out"] += 1
            self._daily(now)["out"] += 1
        self._update(change)

    def record_friend(self, sender_id: str, change_name: str, name: str = "") -> None:
        def change(now):
            customer = self._customer(sender_id, name, now)
            customer["last_friend_change"] = {"change": change_name, "at": now}
            if change_name == "ACCEPTED":
                self._daily(now)["friend_accepts"] += 1
        self._update(change)

    def record_escalation(self, sender_id: str, reason: str, delivered: bool, paused_until: float) -> None:
        """Persist pause, escalation result and daily count in one transaction."""
        def change(now):
            customer = self._customer(sender_id, "", now)
            # Do not undo an admin block while an in-flight send completes.
            if customer["status"] != "blocked":
                customer["status"] = "paused"
                customer["paused_until"] = paused_until
            customer["escalations"].append({"at": now, "reason": reason, "delivered": delivered})
            self._daily(now)["escalations"] += 1
        self._update(change)

    def set_status(self, sender_id: str, status: str, *, paused_until: float | None = None) -> None:
        if status not in {"active", "paused", "blocked"}:
            raise ValueError("Invalid customer status")
        def change(now):
            customer = self._customer(sender_id, "", now)
            customer["status"] = status
            customer["paused_until"] = paused_until if status == "paused" else None
        self._update(change)

    def set_paused_all(self, paused: bool) -> None:
        self._update(lambda now: self._data.update(paused_all=bool(paused)))
