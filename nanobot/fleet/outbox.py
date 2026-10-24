"""Delivery of outbox events to Fleet Manager's inbox (custom; plan native-parity.md 3.3.4, 3.3.15, P1b).

One task per bot drains the journal's outbox: it POSTs each pending event to the binding's inbox_url,
signed with the binding's token, and acts on the answer as the plan's table says:

    2xx (a duplicate too)                     delivered: the row goes
    network error, timeout, 408, 429, 5xx     the whole channel waits: back-off doubles up to 5 minutes,
                                              Retry-After wins; the rest of the pass is not tried
    401 clock_skew                            the clock offset is corrected from server_time, then retried
    401 unknown_token / bad_signature / ...   delivery pauses until a claim brings a new token
    410                                       this bot left that fleet: its pending events are dropped
    400, 403, 404, 409, 413, 422              dead letter: retrying cannot help; kept 30 days for inspection

A failure that says Fleet Manager cannot take anything right now holds the channel, not just the event:
trying the next event would meet the same timeout, and holding keeps the events in order. An event
pending for 7 days becomes a dead letter too. Nothing is dropped silently.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from loguru import logger

from nanobot.fleet.journal import FleetJournal, OutboxEvent
from nanobot.fleet.signing import (
    HEADER_BOT_ID,
    HEADER_FLEET_ID,
    INBOX_PATH,
    canonical_json,
    signed_headers,
)

MAX_BACKOFF_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 15
IDLE_POLL_SECONDS = 30  # a safety net; enqueue() and bind() wake the worker at once
DEAD_LETTER_STATUSES = frozenset({400, 403, 404, 409, 413, 422})
TOKEN_ERRORS = frozenset({"unknown_token", "token_expired", "bad_signature", "missing_auth"})


class FleetOutbox:
    """Drains the journal's outbox to the bound Fleet Manager."""

    def __init__(self, journal: FleetJournal, *, bot_id: str = "", client_factory=None) -> None:
        self.journal = journal
        self.bot_id = bot_id  # this bot's own id: the X-Bot-ID when the binding does not name one
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS))
        self._wake = asyncio.Event()
        self._paused_token_id: int | None = None  # the token Fleet Manager refused; a new one resumes
        self._clock_offset = 0.0  # added to our clock when Fleet Manager says ours is off
        self._hold_until = 0.0  # Fleet Manager could not take events: nothing is sent before this
        self._failures = 0  # in a row, for the back-off; any delivery resets it
        self.last_delivery_error: str | None = None
        self.delivered_total = 0

    # ── control ──────────────────────────────────────────────────────

    def wake(self) -> None:
        """Something changed (an event queued, a new binding): deliver now rather than at the next poll."""
        self._wake.set()

    def bound(self) -> None:
        """A claim brought a (new) token, or the binding went: a pause or a hold ends."""
        self._paused_token_id = None
        self._hold_until = 0.0
        self._failures = 0
        self.last_delivery_error = None
        self.wake()

    def status(self) -> dict[str, Any]:
        """The fleet_channel block of /api/health (plan 3.3.6)."""
        counts = self.journal.outbox_counts()
        return {"outbox_depth": counts["pending"], "dead_letters": counts["dead"],
                "last_delivery_error": self.last_delivery_error,
                "paused": self._paused_token_id is not None,
                "retry_in": max(0, round(self._hold_until - time.time()))}

    # ── the worker ───────────────────────────────────────────────────

    async def run(self) -> None:
        """Deliver forever; cancelled with the gateway."""
        while True:
            try:
                await self.deliver_pending()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # the worker outlives any one bad event
                logger.warning("[Fleet] Outbox pass failed: {}", e)
            now = time.time()
            due = self.journal.next_attempt_at()
            wait = IDLE_POLL_SECONDS if due is None else max(0.0, due - now)
            wait = max(wait, self._hold_until - now)
            if self._paused_token_id is not None:
                wait = IDLE_POLL_SECONDS
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=min(IDLE_POLL_SECONDS, wait))
            except asyncio.TimeoutError:
                pass

    async def deliver_pending(self, *, now: float | None = None) -> int:
        """One pass over the due events, in order. Returns how many were delivered."""
        now = time.time() if now is None else now
        expired = self.journal.expire_pending(now=now)
        if expired:
            logger.warning("[Fleet] {} outbox event(s) waited over 7 days and became dead letters", expired)
        binding = self.journal.binding()
        if binding is None:
            return 0
        if self._paused_token_id is not None and self._paused_token_id == binding.token_id:
            return 0  # still the token Fleet Manager refused; wait for the next claim
        if now < self._hold_until:
            return 0  # Fleet Manager could not take events a moment ago; wait out the back-off
        delivered = 0
        async with self._client_factory() as client:
            for event in self.journal.pending(now=now):
                if event.fleet_id != binding.fleet_id:
                    # Queued for a fleet this bot has since left: never sent to the next fleet (plan 3.3.1)
                    self.journal.drop_fleet(event.fleet_id)
                    logger.info("[Fleet] Dropped the pending events of former fleet '{}'", event.fleet_id)
                    continue
                outcome = await self._attempt(client, binding, event)
                if outcome == "delivered":
                    delivered += 1
                elif outcome != "continue":
                    break  # held, paused, or the fleet is gone: the rest can wait
        self.delivered_total += delivered
        return delivered

    async def _attempt(self, client: httpx.AsyncClient, binding, event: OutboxEvent) -> str:
        body = canonical_json(event.payload)
        headers = {
            "Content-Type": "application/json",
            # Fleet Manager files events, and derives the token, by its own id for this bot
            HEADER_BOT_ID: binding.bot_id or self.bot_id,
            HEADER_FLEET_ID: binding.fleet_id,
            **signed_headers(binding.token, binding.token_id, "POST", INBOX_PATH, body,
                             sent_at=time.time() + self._clock_offset, protocol_version=binding.protocol_version),
        }
        try:
            response = await client.post(binding.inbox_url, content=body, headers=headers)
        except (httpx.HTTPError, OSError) as e:
            self._hold(event, None, f"{type(e).__name__}: {e}")
            return "hold"

        status = response.status_code
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        error = str(data.get("error") or "")

        if 200 <= status < 300:
            self.journal.delivered(event.event_id)
            self.last_delivery_error = None
            self._failures = 0
            self._hold_until = 0.0  # Fleet Manager takes events again
            logger.debug("[Fleet] Delivered {} {} (seq {})", event.kind, event.event_id, event.seq)
            return "delivered"
        if status == 401 and error == "clock_skew":
            server_time = data.get("server_time")
            if isinstance(server_time, (int, float)):
                self._clock_offset = float(server_time) - time.time()
                logger.warning("[Fleet] Fleet Manager's clock differs by {:+.0f}s; correcting", self._clock_offset)
            if event.attempts == 0:
                # Retry at once with the corrected clock (the next pass); after that, back off
                self.journal.defer(event.event_id, next_attempt_at=time.time(), status=status, error="clock_skew")
                return "continue"
            self._hold(event, status, "clock_skew")
            return "hold"
        if status == 401:
            self._paused_token_id = binding.token_id
            self.last_delivery_error = f"401 {error or 'unauthorized'}: waiting for a new token from the next claim"
            logger.warning("[Fleet] Fleet Manager refused token {} ({}); delivery paused until the next claim",
                           binding.token_id, error or "unauthorized")
            return "paused"
        if status == 410:
            dropped = self.journal.drop_fleet(binding.fleet_id)
            self.last_delivery_error = f"410 {error}: this bot no longer belongs to fleet '{binding.fleet_id}'"
            logger.warning("[Fleet] Fleet Manager says this bot left fleet '{}'; dropped {} pending event(s)",
                           binding.fleet_id, dropped)
            return "gone"
        if status in DEAD_LETTER_STATUSES:
            self.journal.dead_letter(event.event_id, status=status, error=error or f"HTTP {status}")
            self.last_delivery_error = f"{status} {error}".strip()
            logger.warning("[Fleet] Event {} ({}) is a dead letter: HTTP {} {}", event.event_id, event.kind, status, error)
            return "continue"
        # 408, 429, 5xx and anything unexpected: Fleet Manager cannot take events right now
        self._hold(event, status, error or f"HTTP {status}", retry_after=_retry_after(response.headers.get("Retry-After")))
        return "hold"

    def _hold(self, event: OutboxEvent, status: int | None, error: str, *, retry_after: float | None = None) -> None:
        """Hold the whole channel: back-off doubles with each failure in a row, Retry-After wins."""
        self._failures += 1
        delay = retry_after if retry_after is not None else min(MAX_BACKOFF_SECONDS, 2.0 ** min(self._failures, 12))
        until = time.time() + delay
        self._hold_until = until
        self.journal.defer(event.event_id, next_attempt_at=until, status=status, error=error)
        self.last_delivery_error = f"{status} {error}".strip() if status else error
        logger.info("[Fleet] Fleet Manager could not take event {} ({}); retrying in {:.0f}s",
                    event.event_id, error, delay)


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
