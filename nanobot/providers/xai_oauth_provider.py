"""xAI (Grok) OAuth-backed provider.

⚠️⚠️⚠️ EXPERIMENTAL — READ BEFORE SHIPPING ⚠️⚠️⚠️
================================================================================
Authenticates to xAI via OAuth at auth.x.ai using the UPSTREAM GROK-CLI
client_id (XAI_OAUTH_CLIENT_ID below). xAI does NOT publish a public OAuth
client-registration portal for third parties (as of 2026-06-12). The only known
way to make this flow work is to reuse the Grok-CLI client_id + grok-cli:access
scope — i.e. impersonate the upstream Grok-CLI client. Hermes does the same and
documents it as temporary "until xAI mints us our own".

The user has knowingly accepted, for EXPERIMENTAL/local use:
  - this may violate xAI Terms of Service;
  - xAI may revoke the client_id / flow at any time;
  - this is NOT a stable production integration.

Legal path for production = the `xai` provider (API key from console.x.ai →
XAI_API_KEY → https://api.x.ai/v1). Prefer that.

A request for a dedicated nanobot client_id is in progress; when granted,
replace XAI_OAUTH_CLIENT_ID.
================================================================================
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from loguru import logger
from oauth_cli_kit.models import OAuthToken

from nanobot.providers.base import LLMResponse
from nanobot.providers.openai_compat_provider import OpenAICompatProvider

XAI_OAUTH_AUTHORIZE_URL = "https://auth.x.ai/oauth2/authorize"
XAI_OAUTH_TOKEN_URL = "https://auth.x.ai/oauth2/token"
XAI_OAUTH_API_BASE = "https://api.x.ai/v1"
# EXPERIMENTAL: upstream Grok-CLI client_id. Replace with a nanobot-owned client_id when xAI grants one.
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_TOKEN_FILENAME = "xai-oauth.json"
XAI_OAUTH_APP_NAME = "nanobot"
XAI_OAUTH_REFERRER = "nanobot-experimental"
XAI_OAUTH_PLAN = "generic"
XAI_OAUTH_REDIRECT_URI = "http://127.0.0.1:56121/callback"

_EXPIRY_SKEW_SECONDS = 60
_refresh_locks: dict[str, asyncio.Lock] = {}


def _storage(credential_profile: str | None = None):
    from nanobot.providers.oauth_profile_storage import get_oauth_token_storage

    return get_oauth_token_storage("xai_oauth", credential_profile)


def _refresh_lock_for(credential_profile: str | None = None) -> asyncio.Lock:
    key = (credential_profile or "").strip()
    lock = _refresh_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _refresh_locks[key] = lock
    return lock


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _pkce_pair() -> tuple[str, str]:
    verifier = _b64url(os.urandom(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def build_xai_oauth_authorize_url(
    *,
    redirect_uri: str = XAI_OAUTH_REDIRECT_URI,
) -> tuple[str, str, str, str, str]:
    """Build the experimental xAI OAuth authorize URL.

    Returns (url, code_verifier, code_challenge, state, nonce).
    """
    verifier, challenge = _pkce_pair()
    state = _b64url(secrets.token_bytes(32))
    nonce = _b64url(secrets.token_bytes(32))
    params = {
        "response_type": "code",
        "client_id": XAI_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": XAI_OAUTH_SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
        "plan": XAI_OAUTH_PLAN,
        "referrer": XAI_OAUTH_REFERRER,
    }
    return (
        f"{XAI_OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}",
        verifier,
        challenge,
        state,
        nonce,
    )


async def exchange_xai_oauth_code(
    *,
    code: str,
    code_verifier: str,
    code_challenge: str,
    redirect_uri: str = XAI_OAUTH_REDIRECT_URI,
    credential_profile: str | None = None,
) -> OAuthToken:
    """Exchange an authorization/displayed code and persist the resulting token."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": XAI_OAUTH_CLIENT_ID,
        "code_verifier": code_verifier,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    async with httpx.AsyncClient(timeout=30.0, trust_env=True) as client:
        response = await client.post(
            XAI_OAUTH_TOKEN_URL,
            headers={"Accept": "application/json"},
            data=data,
        )
    if response.status_code != 200:
        raise RuntimeError(f"xAI token exchange failed: HTTP {response.status_code} — {response.text[:300]}")
    payload = response.json()
    token = _token_from_payload(payload)
    _storage(credential_profile).save(token)
    logger.info("[OAuth/xAI] Token saved. account_id={}", token.account_id or "")
    return token


def get_xai_oauth_login_status(credential_profile: str | None = None) -> OAuthToken | None:
    token = _storage(credential_profile).load()
    if token and token.access:
        return token
    return None


def delete_xai_oauth_token(credential_profile: str | None = None) -> None:
    path = _storage(credential_profile).get_token_path()
    if path.exists():
        path.unlink()


def _token_from_payload(payload: dict[str, Any], previous: OAuthToken | None = None) -> OAuthToken:
    expires_in = int(payload.get("expires_in") or 0)
    expires_at = int(time.time()) + expires_in if expires_in else int(time.time())
    id_payload = _decode_jwt_payload(str(payload.get("id_token") or ""))
    account_id = (
        payload.get("account_id")
        or id_payload.get("email")
        or id_payload.get("sub")
        or (previous.account_id if previous else None)
    )
    refresh = payload.get("refresh_token")
    if not refresh and previous:
        refresh = previous.refresh
    return OAuthToken(
        access=str(payload.get("access_token") or ""),
        refresh=str(refresh or ""),
        expires=expires_at,
        account_id=str(account_id) if account_id else None,
    )


def _is_token_fresh(token: OAuthToken | None) -> bool:
    return bool(token and token.access and token.expires > int(time.time()) + _EXPIRY_SKEW_SECONDS)


async def _refresh_xai_oauth_token(previous: OAuthToken, credential_profile: str | None = None) -> OAuthToken:
    if not previous.refresh:
        raise RuntimeError("xAI OAuth refresh token is missing. Please log in again from the dashboard.")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": previous.refresh,
        "client_id": XAI_OAUTH_CLIENT_ID,
    }
    async with httpx.AsyncClient(timeout=30.0, trust_env=True) as client:
        response = await client.post(
            XAI_OAUTH_TOKEN_URL,
            headers={"Accept": "application/json"},
            data=data,
        )
    if response.status_code in (400, 401):
        raise RuntimeError("xAI OAuth token refresh failed. Please log in again from the dashboard.")
    if response.status_code == 403:
        raise RuntimeError("xAI OAuth entitlement denied. The account may not have API access for this subscription.")
    if response.status_code == 429:
        raise RuntimeError("xAI OAuth quota or rate limit reached.")
    if response.status_code >= 500:
        raise RuntimeError(f"xAI OAuth token service is temporarily unavailable: HTTP {response.status_code}")
    response.raise_for_status()
    token = _token_from_payload(response.json(), previous=previous)
    _storage(credential_profile).save(token)
    return token


async def get_fresh_xai_oauth_token(credential_profile: str | None = None) -> OAuthToken:
    """Load a fresh token, refreshing under lock when expired.

    The token is read from disk for every request; it is not part of provider
    snapshot state.
    """
    token = get_xai_oauth_login_status(credential_profile)
    if _is_token_fresh(token):
        return token  # type: ignore[return-value]
    if not token:
        raise RuntimeError("xAI OAuth is not logged in. Log in from the dashboard or use the xai API-key provider.")
    async with _refresh_lock_for(credential_profile):
        reread = get_xai_oauth_login_status(credential_profile)
        if _is_token_fresh(reread):
            return reread  # type: ignore[return-value]
        return await _refresh_xai_oauth_token(reread or token, credential_profile)


class XaiOAuthProvider(OpenAICompatProvider):
    """Experimental xAI OAuth provider using xAI's OpenAI-compatible API."""

    def __init__(
        self,
        default_model: str = "grok-4.3",
        api_base: str | None = None,
        credential_profile: str | None = None,
    ):
        from nanobot.providers.oauth_profile_storage import require_provider_profile_wave
        from nanobot.providers.registry import find_by_name

        require_provider_profile_wave("xai_oauth", credential_profile)
        self.credential_profile = credential_profile

        super().__init__(
            api_key="no-key",
            api_base=api_base or XAI_OAUTH_API_BASE,
            default_model=default_model,
            spec=find_by_name("xai_oauth"),
        )

    async def _refresh_client_api_key(self) -> str:
        token = await get_fresh_xai_oauth_token(self.credential_profile)
        self.api_key = token.access
        self._client.api_key = token.access
        return token.access

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        try:
            await self._refresh_client_api_key()
            return await super().chat(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                tool_choice=tool_choice,
            )
        except Exception as e:
            return self._handle_error(e)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        try:
            await self._refresh_client_api_key()
            return await super().chat_stream(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                tool_choice=tool_choice,
                on_content_delta=on_content_delta,
            )
        except Exception as e:
            return self._handle_error(e)

    @staticmethod
    def _handle_error(e: Exception) -> LLMResponse:
        status = getattr(getattr(e, "response", None), "status_code", None)
        body = getattr(getattr(e, "response", None), "text", "") or str(e)
        body_lower = body.lower()
        if status == 401 or "invalid_grant" in body_lower or "invalid_token" in body_lower:
            msg = "Error: xAI OAuth authentication failed. Please log in again from the dashboard."
        elif status == 403:
            msg = "Error: xAI OAuth entitlement denied. The account may not have subscription/API access."
        elif status == 429 or "quota" in body_lower or "rate limit" in body_lower:
            msg = "Error: xAI OAuth quota or rate limit reached."
        elif isinstance(status, int) and status >= 500:
            msg = f"Error: xAI OAuth/API transient server error HTTP {status}."
        else:
            msg = f"Error calling xAI OAuth provider: {str(e)[:500]}"
        return LLMResponse(content=msg, finish_reason="error")
