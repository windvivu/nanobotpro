"""In-memory token usage accounting for dashboard telemetry."""

from __future__ import annotations

import hashlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


def _safe_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def public_session_ref(channel: str | None, chat_id: str | None, session_key: str | None = None) -> str:
    """Return a stable public session reference without exposing raw chat identifiers."""
    public_channel = (channel or "unknown").strip() or "unknown"
    raw = f"{public_channel}\0{chat_id or ''}\0{session_key or ''}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{public_channel}:{digest}"


@dataclass(slots=True)
class TokenUsageEvent:
    ts_ms: int
    session_ref: str
    channel: str
    provider: str
    model: str
    model_preset: str | None
    fallback_name: str | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts_ms": self.ts_ms,
            "session_ref": self.session_ref,
            "channel": self.channel,
            "provider": self.provider,
            "model": self.model,
            "model_preset": self.model_preset,
            "fallback_name": self.fallback_name,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "source": self.source,
        }


@dataclass(slots=True)
class TokenUsageBucket:
    key: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    turn_count: int = 0

    def add(self, event: TokenUsageEvent) -> None:
        self.prompt_tokens += event.prompt_tokens
        self.completion_tokens += event.completion_tokens
        self.total_tokens += event.total_tokens
        self.cached_tokens += event.cached_tokens
        self.turn_count += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "turn_count": self.turn_count,
        }


@dataclass(slots=True)
class TokenUsageTotals:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    turn_count: int = 0
    reported_turn_count: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "turn_count": self.turn_count,
            "reported_turn_count": self.reported_turn_count,
        }


@dataclass(slots=True)
class TokenUsageTracker:
    max_recent: int = 200
    _totals: TokenUsageTotals = field(default_factory=TokenUsageTotals)
    _by_provider: dict[str, TokenUsageBucket] = field(default_factory=dict)
    _by_model: dict[str, TokenUsageBucket] = field(default_factory=dict)
    _recent: deque[TokenUsageEvent] = field(init=False)

    def __post_init__(self) -> None:
        self._recent = deque(maxlen=max(1, self.max_recent))

    def record(
        self,
        *,
        usage: dict[str, Any] | None,
        channel: str | None,
        chat_id: str | None,
        session_key: str | None,
        provider: str | None,
        model: str | None,
        model_preset: str | None = None,
        fallback_name: str | None = None,
    ) -> TokenUsageEvent:
        usage = usage or {}
        prompt_tokens = _safe_int(usage.get("prompt_tokens"))
        completion_tokens = _safe_int(usage.get("completion_tokens"))
        total_tokens = _safe_int(usage.get("total_tokens"))
        if total_tokens == 0:
            total_tokens = prompt_tokens + completion_tokens
        cached_tokens = _safe_int(
            usage.get("cached_tokens")
            or usage.get("cache_read_input_tokens")
            or usage.get("cache_creation_input_tokens")
        )
        source = "provider" if any((prompt_tokens, completion_tokens, total_tokens, cached_tokens)) else "empty"
        public_channel = (channel or "unknown").strip() or "unknown"
        event = TokenUsageEvent(
            ts_ms=int(time.time() * 1000),
            session_ref=public_session_ref(public_channel, chat_id, session_key),
            channel=public_channel,
            provider=(provider or "unknown").strip() or "unknown",
            model=(model or "unknown").strip() or "unknown",
            model_preset=model_preset,
            fallback_name=fallback_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cached_tokens=cached_tokens,
            source=source,
        )

        self._totals.turn_count += 1
        if source == "provider":
            self._totals.reported_turn_count += 1
            self._totals.prompt_tokens += prompt_tokens
            self._totals.completion_tokens += completion_tokens
            self._totals.total_tokens += total_tokens
            self._totals.cached_tokens += cached_tokens
            self._bucket(self._by_provider, event.provider).add(event)
            self._bucket(self._by_model, event.model).add(event)
        self._recent.append(event)
        return event

    def snapshot(self) -> dict[str, Any]:
        return {
            "totals": self._totals.as_dict(),
            "by_provider": [bucket.as_dict() for bucket in self._by_provider.values()],
            "by_model": [bucket.as_dict() for bucket in self._by_model.values()],
            "recent": [event.as_dict() for event in list(self._recent)[-20:]],
        }

    @staticmethod
    def _bucket(buckets: dict[str, TokenUsageBucket], key: str) -> TokenUsageBucket:
        bucket = buckets.get(key)
        if bucket is None:
            bucket = TokenUsageBucket(key=key)
            buckets[key] = bucket
        return bucket


def empty_token_usage_snapshot() -> dict[str, Any]:
    return TokenUsageTracker().snapshot()
