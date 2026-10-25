"""Workspace-scoped CSKH state. No bridge, bus, or conversation text is stored here."""

from __future__ import annotations

import copy
import json
import os
import re
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

# Customers simulated from the dashboard to try the bot. Real Zalo ids are numeric, so a
# message from Zalo can never carry this prefix.
TEST_CUSTOMER_PREFIX = "test-"


# No "_": a group's pictures are named <id>_<ms>.<ext>, and the page and the retention sweep split
# the id off at the first "_" (review 15 #4)
_GROUP_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def valid_group_id(group_id) -> bool:
    """Zalo group ids are digit strings; anything else is refused at the door (review 12 #9)."""
    return isinstance(group_id, str) and bool(_GROUP_ID.match(group_id))


def is_test_customer(customer_id) -> bool:
    return str(customer_id).startswith(TEST_CUSTOMER_PREFIX)


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
    # Set by the admin's Resume: escalations before it no longer hold back a new one.
    escalation_cleared_at: float | None = None
    # When the customer last wrote to the bot privately: a group member chatting in a group is not
    # "chatting with the bot" on the Customers tab (custom)
    last_private_at: float | None = None
    inbound_day: str = ""
    inbound_today: int = Field(default=0, ge=0)
    day_notice_sent: bool = False
    images_day: str = ""
    images_today: int = Field(default=0, ge=0)


class _Group(BaseModel):
    """A Zalo group the bot was added to. Ignored until the admin allows it (custom)."""
    model_config = ConfigDict(extra="allow")
    name: str = ""
    last_sender: str = ""
    first_seen: float
    last_seen: float
    messages_in: int = Field(default=0, ge=0)
    messages_out: int = Field(default=0, ge=0)
    allowed: bool = False
    mode: Literal["mention", "ambient"] = "mention"
    inbound_day: str = ""
    inbound_today: int = Field(default=0, ge=0)
    # Staff is handling the group (a reply from the CSKH page, a handoff): the bot is quiet until then
    held_until: float | None = None
    escalations: list[dict] = Field(default_factory=list)
    # Set by the admin's Resume, as for a customer
    escalation_cleared_at: float | None = None
    # Zalo told the bot it was added (joined_at), by whom, or taken out (left_at)
    joined_at: float | None = None
    left_at: float | None = None
    added_by: str = ""
    # The admin set the group aside: the bot stays silent there, and it no longer waits for a decision
    dismissed: bool = False
    # When a member last wrote in the group: "active now" on the page
    last_message_at: float | None = None


class _Daily(BaseModel):
    new_customers: int = Field(default=0, ge=0)
    inbound: int = Field(default=0, ge=0, alias="in")
    outbound: int = Field(default=0, ge=0, alias="out")
    limited: int = Field(default=0, ge=0)
    escalations: int = Field(default=0, ge=0)
    friend_accepts: int = Field(default=0, ge=0)
    blocked_dropped: int = Field(default=0, ge=0)
    # Bot replies and the customers' total wait for them: today's average reply time (custom)
    replies: int = Field(default=0, ge=0)
    reply_ms: int = Field(default=0, ge=0)


class _State(BaseModel):
    customers: dict[str, _Customer] = Field(default_factory=dict)
    groups: dict[str, _Group] = Field(default_factory=dict)
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
            self._count(now, "new_customers", sender_id)
        if name:
            customer["name"] = name
        customer["last_seen"] = now
        return customer

    def _count(self, now: float, key: str, sender_id: str) -> None:
        """Today's totals describe real customers; dashboard test customers stay out."""
        if not is_test_customer(sender_id):
            self._daily(now)[key] += 1

    def _prune(self, now: float) -> None:
        oldest_day = datetime.fromtimestamp(now, self._timezone).date() - timedelta(days=89)
        self._data["daily"] = {key: value for key, value in self._data["daily"].items()
                               if date.fromisoformat(key) >= oldest_day}
        self._data["customers"] = {key: value for key, value in self._data["customers"].items()
                                   if value["last_seen"] >= now - 180 * 86400}
        self._data["groups"] = {key: value for key, value in self._data.get("groups", {}).items()
                                if value["last_seen"] >= now - 180 * 86400 or value["allowed"]}
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

    def admit_inbound(self, sender_id: str, name: str, *, per_minute: int, per_day: int,
                      group: bool = False) -> tuple[bool, str | None]:
        """Persist one receive decision before publishing to the bus.

        Returns (allowed, notice_kind). notice_kind "paused": the customer waits for staff and
        the message is within quota, so the caller keeps it for staff without a bot turn.
        Groups must be dropped by the caller.
        All non-paused/non-blocked attempts count toward quotas, including spam.
        Notice flags reserve one attempt; a failed send must not trigger a flood.
        """
        def change(now):
            status = self.customer_status(sender_id)
            if status == "blocked":
                self._count(now, "blocked_dropped", sender_id)
                # Recent activity keeps a blocked customer from expiring silently.
                self._data["customers"][sender_id]["last_seen"] = now
                return False, None
            customer = self._customer(sender_id, name, now)
            customer["messages_in"] += 1
            if not group:
                customer["last_private_at"] = now
            self._count(now, "in", sender_id)
            if status != "paused" and customer["status"] == "paused":
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
                return (False, "paused") if status == "paused" else (True, None)
            customer["limited_count"] += 1
            self._count(now, "limited", sender_id)
            return False, notice

        return self._update(change)

    # ── Groups (custom): seen while ignored, served once allowed ──
    def _group(self, group_id: str, now: float) -> dict:
        if not valid_group_id(group_id):
            raise ValueError("Invalid group id")
        group = self._data.setdefault("groups", {}).get(group_id)
        if group is None:
            group = _Group(first_seen=now, last_seen=now).model_dump()
            self._data["groups"][group_id] = group
        group["last_seen"] = now
        return group

    def record_group_seen(self, group_id: str, sender_name: str = "") -> None:
        """A message arrived in a group: the admin can see the group and decide to allow it."""
        def change(now):
            group = self._group(group_id, now)
            group["last_message_at"] = now
            group["left_at"] = None  # a message reached the bot: it is in the group
            if sender_name:
                group["last_sender"] = sender_name
        self._update(change)

    def record_group_joined(self, group_id: str, name: str = "", added_by: str = "") -> None:
        """Zalo says the bot was added to a group (custom): the page lists it at once, waiting for the
        admin to allow it or set it aside. Zalo's name for it fills in a name the admin has not set."""
        def change(now):
            group = self._group(group_id, now)
            group.update(joined_at=now, left_at=None)
            if name.strip() and not group.get("name"):
                group["name"] = name.strip()[:100]
            if added_by.strip():
                group["added_by"] = added_by.strip()[:100]
        self._update(change)

    def forget_group(self, group_id: str) -> None:
        """The admin removes a group from the page (custom), e.g. one that was disbanded. It comes back
        if a message from it reaches the bot again."""
        self._update(lambda now: self._data.get("groups", {}).pop(group_id, None))

    def record_group_left(self, group_id: str) -> None:
        """The bot was taken out of the group, or left it (custom): it no longer waits for a decision."""
        def change(now):
            self._group(group_id, now)["left_at"] = now
        self._update(change)

    def group_access(self, group_id: str) -> tuple[bool, str]:
        """(allowed, mode) for a group; a group never seen is not allowed."""
        group = self._data.get("groups", {}).get(group_id)
        if group is None:
            return False, "mention"
        return bool(group["allowed"]), group["mode"]

    def set_group(self, group_id: str, *, allowed: bool | None = None, mode: str | None = None,
                  name: str | None = None, dismissed: bool | None = None) -> None:
        if mode is not None and mode not in {"mention", "ambient"}:
            raise ValueError("Invalid group mode")
        def change(now):
            group = self._group(group_id, now)
            if allowed is not None:
                group["allowed"] = bool(allowed)
                # Revoking is a decision too: the group no longer waits for one (seen live: a revoked
                # group came back as "new"); allowing after all clears it
                group["dismissed"] = not allowed
            if dismissed is not None:
                group["dismissed"] = bool(dismissed)
            if mode is not None:
                group["mode"] = mode
            if name is not None:
                group["name"] = name.strip()[:100]
        self._update(change)

    def admit_group_inbound(self, group_id: str, sender_name: str, *, per_day: int) -> bool:
        """Count one group message; False once the group's daily cap is reached."""
        def change(now):
            group = self._group(group_id, now)
            if sender_name:
                group["last_sender"] = sender_name
            group["messages_in"] += 1
            today = self._day(now)
            if group["inbound_day"] != today:
                group.update(inbound_day=today, inbound_today=0)
            group["inbound_today"] += 1
            return group["inbound_today"] <= per_day
        return self._update(change)

    def record_group_outbound(self, group_id: str) -> None:
        def change(now):
            self._group(group_id, now)["messages_out"] += 1
        self._update(change)

    def has_customer(self, sender_id: str) -> bool:
        """Whether this customer is known, without copying the whole state (custom): the CSKH page
        asks for every transcript poll and every picture."""
        with self._lock:
            return sender_id in self._data["customers"]

    # ── Groups staff handle from the CSKH page (custom) ──
    def group(self, group_id: str) -> dict | None:
        """A copy of one group, or None: without copying the whole state."""
        with self._lock:
            group = self._data.get("groups", {}).get(group_id)
            return copy.deepcopy(group) if group is not None else None

    def group_status(self, group_id: str) -> str:
        """"paused" while staff handles the group or everything is paused: the bot stays quiet there
        and every message is kept for staff; else "active"."""
        with self._lock:
            if self._data["paused_all"]:
                return "paused"
            until = (self._data.get("groups", {}).get(group_id) or {}).get("held_until")
            return "paused" if until and until > self._clock() else "active"

    def _known_group(self, group_id: str) -> dict:
        group = self._data.get("groups", {}).get(group_id)
        if group is None:  # a group exists once a message from it arrived
            raise ValueError("Unknown group")
        return group

    def hold_group_for_staff(self, group_id: str, until: float) -> None:
        """Staff answered in the group: the bot stays quiet there until then, or until Resume."""
        def change(now):
            group = self._known_group(group_id)
            group["held_until"] = max(until, group.get("held_until") or 0)
        self._update(change)

    def set_group_hold(self, group_id: str, held_until: float | None) -> None:
        """Put back what a staff send found, when Zalo took none of it."""
        self._update(lambda now: self._known_group(group_id).update(held_until=held_until))

    def record_group_escalation(self, group_id: str, member_id: str, member_name: str, reason: str,
                                delivered: bool, held_until: float) -> None:
        """A member asked for a person in the group: the whole group waits for staff, in one write."""
        def change(now):
            group = self._known_group(group_id)
            group["held_until"] = max(held_until, group.get("held_until") or 0)  # never shorter (review 15 #2)
            entries = group.setdefault("escalations", [])
            entries.append({"at": now, "reason": reason, "delivered": delivered,
                            "member": member_id, "member_name": member_name[:100]})
            del entries[:-50]  # an allowed group is never pruned: keep its recent handoffs only
            self._count(now, "escalations", group_id)
        self._update(change)

    def resume_group(self, group_id: str) -> None:
        """Admin Resume: the bot answers in the group again, and may hand it over again at once."""
        def change(now):
            group = self._known_group(group_id)
            group["held_until"] = None
            group["escalation_cleared_at"] = now
        self._update(change)

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
        """One message from a customer, privately."""
        def change(now):
            customer = self._customer(sender_id, name, now)
            customer["messages_in"] += 1
            customer["last_private_at"] = now
            self._count(now, "in", sender_id)
        self._update(change)

    def admit_image(self, sender_id: str, *, per_day: int) -> bool:
        """Count one picture from a customer; False once today's allowance is spent (custom):
        every picture is a vision call, and the text quota alone does not bound that."""
        def change(now):
            customer = self._customer(sender_id, "", now)
            today = self._day(now)
            if customer.get("images_day") != today:
                customer.update(images_day=today, images_today=0)
            customer["images_today"] = customer.get("images_today", 0) + 1
            return customer["images_today"] <= per_day
        return self._update(change)

    def record_reply_time(self, sender_id: str, waited_ms: int) -> None:
        """One bot reply and how long the customer waited for it, for today's average."""
        def change(now):
            if not is_test_customer(sender_id):
                daily = self._daily(now)
                daily["replies"] = daily.get("replies", 0) + 1
                daily["reply_ms"] = daily.get("reply_ms", 0) + max(0, int(waited_ms))
        self._update(change)

    def record_outbound(self, sender_id: str) -> None:
        def change(now):
            self._customer(sender_id, "", now)["messages_out"] += 1
            self._count(now, "out", sender_id)
        self._update(change)

    def record_friend(self, sender_id: str, change_name: str, name: str = "") -> None:
        def change(now):
            customer = self._customer(sender_id, name, now)
            customer["last_friend_change"] = {"change": change_name, "at": now}
            if change_name == "ACCEPTED":
                self._count(now, "friend_accepts", sender_id)
        self._update(change)

    def record_escalation(self, sender_id: str, reason: str, delivered: bool, paused_until: float) -> None:
        """Persist pause, escalation result and daily count in one transaction."""
        def change(now):
            customer = self._customer(sender_id, "", now)
            # Do not undo an admin block while an in-flight send completes.
            if customer["status"] != "blocked":
                # Nor cut short a longer wait already set: staff answered while this turn ran, or the
                # pause was shortened since (review 15 #2). A pause without an end stays one.
                endless = customer["status"] == "paused" and customer.get("paused_until") is None
                customer["status"] = "paused"
                if not endless:
                    customer["paused_until"] = max(paused_until, customer.get("paused_until") or 0)
            customer["escalations"].append({"at": now, "reason": reason, "delivered": delivered})
            self._count(now, "escalations", sender_id)
        self._update(change)

    def set_status(self, sender_id: str, status: str, *, paused_until: float | None = None) -> None:
        if status not in {"active", "paused", "blocked"}:
            raise ValueError("Invalid customer status")
        def change(now):
            customer = self._customer(sender_id, "", now)
            customer["status"] = status
            customer["paused_until"] = paused_until if status == "paused" else None
        self._update(change)

    def resume_customer(self, sender_id: str) -> None:
        """Admin resume: the customer is active again and may be escalated again at once."""
        def change(now):
            customer = self._customer(sender_id, "", now)
            customer["status"] = "active"
            customer["paused_until"] = None
            customer["escalation_cleared_at"] = now
        self._update(change)

    def hold_for_staff(self, sender_id: str, until: float) -> None:
        """Staff is talking to this customer: the bot stays quiet until then, or until Resume."""
        def change(now):
            customer = self._customer(sender_id, "", now)
            if customer["status"] != "blocked":
                customer["status"] = "paused"
                customer["paused_until"] = max(until, customer.get("paused_until") or 0)
        self._update(change)

    def set_paused_all(self, paused: bool) -> None:
        self._update(lambda now: self._data.update(paused_all=bool(paused)))
