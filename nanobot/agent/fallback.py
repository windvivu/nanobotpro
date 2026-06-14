"""Runtime fallback helpers for model/provider errors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from nanobot.providers.base import LLMProvider, LLMResponse


@dataclass(slots=True)
class FallbackRuntime:
    """Resolved fallback provider and request settings."""

    name: str
    provider: LLMProvider
    model: str
    max_tokens: int
    temperature: float
    reasoning_effort: str | None = None


FallbackFactory = Callable[[], list[FallbackRuntime]]


_AUTH_ERROR_MARKERS = (
    "auth",
    "unauthorized",
    "forbidden",
    "invalid_api_key",
    "invalid api key",
    "incorrect api key",
    "permission denied",
)
_QUOTA_ERROR_MARKERS = (
    "insufficient_quota",
    "quota_exceeded",
    "quota exceeded",
    "quota exhausted",
    "out of credits",
    "out of credit",
    "insufficient balance",
    "billing",
    "credit",
)


def _error_text(response: LLMResponse) -> str:
    parts = [
        response.error_kind,
        response.error_type,
        response.error_code,
        response.content,
    ]
    return " ".join(str(part).lower() for part in parts if part)


def _is_auth_or_config_error(response: LLMResponse) -> bool:
    if response.error_status_code in {401, 403}:
        return True
    text = _error_text(response)
    return any(marker in text for marker in _AUTH_ERROR_MARKERS)


def _is_quota_or_credit_error(response: LLMResponse) -> bool:
    text = _error_text(response)
    return any(marker in text for marker in _QUOTA_ERROR_MARKERS)


def should_try_fallback(response: LLMResponse) -> bool:
    """Return True when a final provider error should attempt fallback."""

    if response.finish_reason != "error":
        return False
    if _is_auth_or_config_error(response):
        return False
    if LLMProvider._is_transient_response(response):
        return True
    return _is_quota_or_credit_error(response)
