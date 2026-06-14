"""OAuth login routes for OpenAI Codex, xAI OAuth, and GitHub Copilot."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger

router = APIRouter()

# ---------------------------------------------------------------------------
# In-memory session store for active OAuth/device flows
# { session_id: {"type": "codex"|"copilot", "status": ..., "data": ..., "expires": int} }
# ---------------------------------------------------------------------------
_sessions: dict[str, dict[str, Any]] = {}
_SESSION_TTL = 600  # 10 minutes


def _prune_sessions() -> None:
    now = time.time()
    expired = [k for k, v in _sessions.items() if v.get("expires", 0) < now]
    for k in expired:
        del _sessions[k]


def _new_session(typ: str, data: dict[str, Any]) -> str:
    import uuid
    _prune_sessions()
    sid = uuid.uuid4().hex
    _sessions[sid] = {"type": typ, "status": "pending", "data": data, "expires": time.time() + _SESSION_TTL}
    return sid


async def _optional_json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _validate_oauth_credential_profile(
    expected_provider: str,
    credential_profile: str | None,
) -> str | None:
    profile_name = (credential_profile or "").strip() or None
    if profile_name is None:
        return None

    from nanobot.config.loader import load_config

    selection = load_config().resolve_provider_selection(
        provider_override=expected_provider,
        credential_profile=profile_name,
    )
    if selection.provider_name != expected_provider:
        raise ValueError(
            f"credential_profile '{profile_name}' belongs to provider "
            f"'{selection.provider_name}', not '{expected_provider}'"
        )
    return profile_name


def _require_session_profile_wave(session: dict[str, Any], expected_provider: str) -> str | None:
    profile_name = _validate_oauth_credential_profile(
        expected_provider,
        session.get("data", {}).get("credential_profile"),
    )
    from nanobot.providers.oauth_profile_storage import require_provider_profile_wave

    require_provider_profile_wave(expected_provider, profile_name)
    return profile_name


# ===========================================================================
# OpenAI Codex — Manual URL Flow
# ===========================================================================

def _build_codex_auth_url() -> tuple[str, str, str]:
    """Build PKCE authorization URL for Codex. Returns (url, verifier, state)."""
    import base64
    import hashlib
    import os
    import urllib.parse

    from oauth_cli_kit import OPENAI_CODEX_PROVIDER as _CODEX

    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()

    params = {
        "response_type": "code",
        "client_id": _CODEX.client_id,
        "redirect_uri": _CODEX.redirect_uri,
        "scope": _CODEX.scope,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": _CODEX.default_originator,
    }
    url = f"{_CODEX.authorize_url}?{urllib.parse.urlencode(params)}"
    return url, verifier, state


async def _exchange_codex_code(
    code: str,
    verifier: str,
    credential_profile: str | None = None,
) -> dict[str, Any]:
    """Exchange authorization code for token. Returns token dict."""
    import httpx
    from oauth_cli_kit import OPENAI_CODEX_PROVIDER as _CODEX

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _CODEX.redirect_uri,
        "client_id": _CODEX.client_id,
        "code_verifier": verifier,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(_CODEX.token_url, data=data)
        if r.status_code != 200:
            raise RuntimeError(f"Token exchange failed: HTTP {r.status_code} — {r.text[:300]}")
        token_data = r.json()

    # Determine account_id from JWT claim
    import base64
    import json as _json
    account_id = ""
    id_token = token_data.get("id_token", "")
    if id_token:
        try:
            payload_b64 = id_token.split(".")[1]
            payload_b64 += "=" * (4 - len(payload_b64) % 4)
            payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
            account_id = payload.get("chatgpt_account_id", "") or payload.get("sub", "")
        except Exception:
            pass

    # Save token via oauth-cli-kit storage
    from oauth_cli_kit.models import OAuthToken

    from nanobot.providers.oauth_profile_storage import get_oauth_token_storage
    expires_in = token_data.get("expires_in", 3600)
    token = OAuthToken(
        access=token_data.get("access_token", ""),
        refresh=token_data.get("refresh_token", ""),
        expires=int(time.time()) + expires_in,
        account_id=account_id,
    )
    storage = get_oauth_token_storage("openai_codex", credential_profile)
    storage.save(token)
    logger.info("[OAuth/Codex] Token saved. account_id={}", account_id)
    return {"account_id": account_id, "expires_in": expires_in}


@router.get("/oauth/codex/status")
async def codex_status(request: Request) -> JSONResponse:
    """Check if Codex token exists and is still valid."""
    try:
        profile_name = _validate_oauth_credential_profile(
            "openai_codex", request.query_params.get("credential_profile")
        )
        from nanobot.providers.oauth_profile_storage import require_provider_profile_wave

        require_provider_profile_wave("openai_codex", profile_name)
    except Exception as e:
        return JSONResponse({"logged_in": False, "error": str(e)}, status_code=409)
    try:
        from nanobot.providers.oauth_profile_storage import get_oauth_token_storage

        storage = get_oauth_token_storage("openai_codex", profile_name)
        token = storage.load()
        if token and token.expires > time.time() + 60:
            return JSONResponse({"logged_in": True, "account_id": token.account_id or ""})
        return JSONResponse({"logged_in": False})
    except Exception as e:
        logger.debug("[OAuth/Codex] Status check error: {}", e)
        return JSONResponse({"logged_in": False})


@router.post("/oauth/codex/start")
async def codex_start(request: Request) -> JSONResponse:
    """Generate Codex OAuth URL for manual flow. Returns url + session_id."""
    try:
        body = await _optional_json(request)
        credential_profile = _validate_oauth_credential_profile(
            "openai_codex", body.get("credential_profile")
        )
        url, verifier, state = _build_codex_auth_url()
        from oauth_cli_kit import OPENAI_CODEX_PROVIDER as _CODEX
        sid = _new_session("codex", {
            "verifier": verifier,
            "state": state,
            "redirect_uri": _CODEX.redirect_uri,
            "credential_profile": credential_profile,
        })
        logger.info("[OAuth/Codex] Started manual flow, session={}", sid)
        return JSONResponse({"success": True, "url": url, "session_id": sid})
    except Exception as e:
        logger.error("[OAuth/Codex] Start error: {}", e)
        return JSONResponse({"success": False, "error": str(e)})


@router.post("/oauth/codex/callback")
async def codex_callback(request: Request) -> JSONResponse:
    """Accept the callback URL pasted by user, extract code and exchange for token."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON"})

    session_id = (body.get("session_id") or "").strip()
    callback_url = (body.get("callback_url") or "").strip()

    if not session_id or session_id not in _sessions:
        return JSONResponse({"success": False, "error": "Session expired or invalid. Please restart login."})

    sess = _sessions[session_id]
    if sess["type"] != "codex":
        return JSONResponse({"success": False, "error": "Wrong session type"})

    try:
        credential_profile = _require_session_profile_wave(sess, "openai_codex")
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=409)

    # Parse callback URL
    import urllib.parse
    try:
        parsed = urllib.parse.urlparse(callback_url)
        params = urllib.parse.parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        state_recv = params.get("state", [None])[0]
    except Exception:
        return JSONResponse({"success": False, "error": "Cannot parse callback URL"})

    if not code:
        return JSONResponse({"success": False, "error": "No authorization code found in URL"})

    if state_recv and state_recv != sess["data"]["state"]:
        return JSONResponse({"success": False, "error": "State mismatch — possible CSRF. Please restart."})

    try:
        result = await _exchange_codex_code(code, sess["data"]["verifier"], credential_profile)
        del _sessions[session_id]
        return JSONResponse({"success": True, "account_id": result.get("account_id", "")})
    except Exception as e:
        logger.error("[OAuth/Codex] Token exchange error: {}", e)
        return JSONResponse({"success": False, "error": str(e)})


@router.post("/oauth/codex/logout")
async def codex_logout(request: Request) -> JSONResponse:
    """Delete cached Codex token."""
    try:
        body = await _optional_json(request)
        profile_name = _validate_oauth_credential_profile(
            "openai_codex", body.get("credential_profile")
        )
        from nanobot.providers.oauth_profile_storage import require_provider_profile_wave

        require_provider_profile_wave("openai_codex", profile_name)
        from nanobot.providers.oauth_profile_storage import get_oauth_token_storage

        storage = get_oauth_token_storage("openai_codex", profile_name)
        token_path = storage.get_token_path()
        if token_path.exists():
            token_path.unlink()
        logger.info("[OAuth/Codex] Token deleted")
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


# ===========================================================================
# xAI OAuth — Experimental manual URL / displayed-code flow
# ===========================================================================


@router.get("/oauth/xai/status")
async def xai_status(request: Request) -> JSONResponse:
    """Check whether an experimental xAI OAuth token exists and is still valid."""
    try:
        profile_name = _validate_oauth_credential_profile(
            "xai_oauth", request.query_params.get("credential_profile")
        )
        from nanobot.providers.oauth_profile_storage import require_provider_profile_wave

        require_provider_profile_wave("xai_oauth", profile_name)
    except Exception as e:
        return JSONResponse(
            {"logged_in": False, "experimental": True, "error": str(e)},
            status_code=409,
        )
    try:
        from nanobot.providers.xai_oauth_provider import get_xai_oauth_login_status

        token = get_xai_oauth_login_status(profile_name)
        if token and token.expires > time.time() + 60:
            return JSONResponse({
                "logged_in": True,
                "account_id": token.account_id or "",
                "experimental": True,
            })
        return JSONResponse({"logged_in": False, "experimental": True})
    except Exception as e:
        logger.debug("[OAuth/xAI] Status check error: {}", e)
        return JSONResponse({"logged_in": False, "experimental": True})


@router.post("/oauth/xai/start")
async def xai_start(request: Request) -> JSONResponse:
    """Generate xAI OAuth URL for experimental manual flow."""
    try:
        body = await _optional_json(request)
        credential_profile = _validate_oauth_credential_profile(
            "xai_oauth", body.get("credential_profile")
        )
        from nanobot.providers.xai_oauth_provider import (
            XAI_OAUTH_REDIRECT_URI,
            build_xai_oauth_authorize_url,
        )

        url, verifier, challenge, state, nonce = build_xai_oauth_authorize_url()
        sid = _new_session("xai", {
            "code_verifier": verifier,
            "code_challenge": challenge,
            "state": state,
            "nonce": nonce,
            "redirect_uri": XAI_OAUTH_REDIRECT_URI,
            "credential_profile": credential_profile,
        })
        logger.warning(
            "[OAuth/xAI] Started EXPERIMENTAL flow using upstream Grok-CLI client_id; session={}",
            sid,
        )
        return JSONResponse({
            "success": True,
            "url": url,
            "session_id": sid,
            "experimental": True,
            "warning": (
                "Experimental xAI OAuth uses the upstream Grok-CLI client_id. "
                "For production, prefer the xai API-key provider."
            ),
        })
    except Exception as e:
        logger.error("[OAuth/xAI] Start error: {}", e)
        return JSONResponse({"success": False, "error": str(e)})


def _parse_xai_callback_or_code(value: str) -> tuple[str | None, str | None, str | None, bool]:
    """Return (code, state, error, is_bare_code) without logging sensitive values."""
    import urllib.parse

    raw = value.strip()
    if not raw:
        return None, None, None, False
    if "://" not in raw and "code=" not in raw:
        return raw, None, None, True
    parsed = urllib.parse.urlparse(raw)
    params = urllib.parse.parse_qs(parsed.query)
    return (
        params.get("code", [None])[0],
        params.get("state", [None])[0],
        params.get("error", [None])[0],
        False,
    )


@router.post("/oauth/xai/callback")
async def xai_callback(request: Request) -> JSONResponse:
    """Accept a full callback URL or bare displayed-code and exchange it for token."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON"})

    session_id = (body.get("session_id") or "").strip()
    callback_url = (body.get("callback_url") or body.get("code") or "").strip()

    if not session_id or session_id not in _sessions:
        return JSONResponse({"success": False, "error": "Session expired or invalid. Please restart login."})

    sess = _sessions[session_id]
    if sess["type"] != "xai":
        return JSONResponse({"success": False, "error": "Wrong session type"})

    try:
        credential_profile = _require_session_profile_wave(sess, "xai_oauth")
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=409)

    try:
        code, state_recv, auth_error, is_bare_code = _parse_xai_callback_or_code(callback_url)
    except Exception:
        return JSONResponse({"success": False, "error": "Cannot parse callback URL/code"})

    if auth_error:
        return JSONResponse({"success": False, "error": f"xAI authorization failed: {auth_error}"})
    if not code:
        return JSONResponse({"success": False, "error": "No authorization code found"})

    state_verified = False
    if is_bare_code:
        logger.warning(
            "[OAuth/xAI] Bare displayed-code fallback used; state cannot be verified. "
            "Continuing only because xAI Grok-CLI client returns displayed codes."
        )
    else:
        if state_recv != sess["data"]["state"]:
            return JSONResponse({"success": False, "error": "State mismatch — possible CSRF. Please restart."})
        state_verified = True

    try:
        from nanobot.providers.xai_oauth_provider import exchange_xai_oauth_code

        token = await exchange_xai_oauth_code(
            code=code,
            code_verifier=sess["data"]["code_verifier"],
            code_challenge=sess["data"]["code_challenge"],
            redirect_uri=sess["data"]["redirect_uri"],
            credential_profile=credential_profile,
        )
        del _sessions[session_id]
        return JSONResponse({
            "success": True,
            "account_id": token.account_id or "",
            "state_verified": state_verified,
            "bare_code": is_bare_code,
            "experimental": True,
            "warning": "Bare-code fallback cannot verify state." if is_bare_code else "",
        })
    except Exception as e:
        logger.error("[OAuth/xAI] Token exchange error: {}", e)
        return JSONResponse({"success": False, "error": str(e)})


@router.post("/oauth/xai/logout")
async def xai_logout(request: Request) -> JSONResponse:
    """Delete cached xAI OAuth token."""
    try:
        body = await _optional_json(request)
        profile_name = _validate_oauth_credential_profile(
            "xai_oauth", body.get("credential_profile")
        )
        from nanobot.providers.oauth_profile_storage import require_provider_profile_wave

        require_provider_profile_wave("xai_oauth", profile_name)
        from nanobot.providers.xai_oauth_provider import delete_xai_oauth_token

        delete_xai_oauth_token(profile_name)
        logger.info("[OAuth/xAI] Token deleted")
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


# ===========================================================================
# GitHub Copilot — Device Authorization Flow
# ===========================================================================

_COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"  # GitHub Copilot CLI client ID (public)
_COPILOT_DEVICE_URL = "https://github.com/login/device/code"
_COPILOT_TOKEN_URL = "https://github.com/login/oauth/access_token"
_COPILOT_API_URL = "https://api.githubcopilot.com"
_COPILOT_LEGACY_TOKEN_FILE = "github_copilot.json"


def _migrate_legacy_copilot_token() -> None:
    """Convert the old dashboard token JSON into the runtime OAuthToken schema."""
    import json
    from datetime import datetime, timezone

    from oauth_cli_kit.models import OAuthToken

    from nanobot.config.paths import get_data_dir
    from nanobot.providers.oauth_profile_storage import get_oauth_token_storage

    storage = get_oauth_token_storage("github_copilot")
    if storage.load() is not None:
        return

    legacy_path = get_data_dir() / _COPILOT_LEGACY_TOKEN_FILE
    if not legacy_path.exists():
        return
    try:
        payload = json.loads(legacy_path.read_text(encoding="utf-8"))
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            return
        storage.save(OAuthToken(
            access=access_token,
            refresh="",
            expires=int((time.time() + 315360000) * 1000),
            account_id=None,
        ))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        legacy_path.replace(legacy_path.with_name(f"{legacy_path.name}.migrated-{stamp}.bak"))
        logger.info("[OAuth/Copilot] Migrated legacy dashboard token to canonical storage")
    except Exception as exc:
        logger.warning("[OAuth/Copilot] Legacy token migration failed: {}", exc)


def _save_copilot_token(access_token: str, credential_profile: str | None = None) -> None:
    """Persist GitHub Copilot access token to disk."""
    from oauth_cli_kit.models import OAuthToken

    from nanobot.providers.oauth_profile_storage import get_oauth_token_storage

    storage = get_oauth_token_storage("github_copilot", credential_profile)
    storage.save(OAuthToken(
        access=access_token,
        refresh="",
        expires=int((time.time() + 315360000) * 1000),
        account_id=None,
    ))
    logger.info("[OAuth/Copilot] Token saved to canonical runtime storage")


def _load_copilot_token(credential_profile: str | None = None) -> str | None:
    """Load GitHub Copilot access token from disk."""
    from nanobot.providers.oauth_profile_storage import get_oauth_token_storage

    if not credential_profile:
        _migrate_legacy_copilot_token()
    token = get_oauth_token_storage("github_copilot", credential_profile).load()
    return token.access if token and token.access else None


def _delete_copilot_token(credential_profile: str | None = None) -> None:
    from nanobot.providers.oauth_profile_storage import get_oauth_token_storage

    token_path = get_oauth_token_storage("github_copilot", credential_profile).get_token_path()
    if token_path.exists():
        token_path.unlink()


@router.get("/oauth/copilot/status")
async def copilot_status(request: Request) -> JSONResponse:
    """Check if Copilot token exists."""
    try:
        profile_name = _validate_oauth_credential_profile(
            "github_copilot", request.query_params.get("credential_profile")
        )
        from nanobot.providers.oauth_profile_storage import require_provider_profile_wave

        require_provider_profile_wave("github_copilot", profile_name)
    except Exception as e:
        return JSONResponse({"logged_in": False, "error": str(e)}, status_code=409)
    token = _load_copilot_token(profile_name)
    if token:
        return JSONResponse({"logged_in": True})
    return JSONResponse({"logged_in": False})


@router.post("/oauth/copilot/start")
async def copilot_start(request: Request) -> JSONResponse:
    """Start GitHub Copilot device flow. Returns user_code and verification_uri."""
    import httpx
    try:
        body = await _optional_json(request)
        credential_profile = _validate_oauth_credential_profile(
            "github_copilot", body.get("credential_profile")
        )
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                _COPILOT_DEVICE_URL,
                headers={"Accept": "application/json"},
                data={"client_id": _COPILOT_CLIENT_ID, "scope": "read:user"},
            )
            if r.status_code != 200:
                return JSONResponse({"success": False, "error": f"GitHub returned HTTP {r.status_code}"})
            data = r.json()

        device_code = data.get("device_code")
        user_code = data.get("user_code")
        verification_uri = data.get("verification_uri", "https://github.com/login/device")
        interval = data.get("interval", 5)
        expires_in = data.get("expires_in", 900)

        if not device_code or not user_code:
            return JSONResponse({"success": False, "error": "No device_code/user_code from GitHub"})

        sid = _new_session("copilot", {
            "device_code": device_code,
            "interval": interval,
            "expires_in": expires_in,
            "credential_profile": credential_profile,
        })
        _sessions[sid]["expires"] = time.time() + expires_in

        logger.info("[OAuth/Copilot] Device flow started, user_code={}", user_code)
        return JSONResponse({
            "success": True,
            "session_id": sid,
            "user_code": user_code,
            "verification_uri": verification_uri,
            "expires_in": expires_in,
            "interval": interval,
        })
    except Exception as e:
        logger.error("[OAuth/Copilot] Start error: {}", e)
        return JSONResponse({"success": False, "error": str(e)})


@router.post("/oauth/copilot/poll")
async def copilot_poll(request: Request) -> JSONResponse:
    """Poll GitHub token endpoint. Frontend calls this repeatedly until done."""
    import httpx
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON"})

    session_id = (body.get("session_id") or "").strip()
    if not session_id or session_id not in _sessions:
        return JSONResponse({"success": False, "done": True, "error": "Session expired. Please restart."})

    sess = _sessions[session_id]
    if sess["type"] != "copilot":
        return JSONResponse({"success": False, "done": True, "error": "Wrong session type"})
    try:
        credential_profile = _require_session_profile_wave(sess, "github_copilot")
    except Exception as e:
        return JSONResponse({"success": False, "done": True, "error": str(e)}, status_code=409)
    device_code = sess["data"]["device_code"]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                _COPILOT_TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": _COPILOT_CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            data = r.json()

        error = data.get("error")
        if error == "authorization_pending":
            return JSONResponse({"success": False, "done": False, "pending": True})
        if error == "slow_down":
            return JSONResponse({"success": False, "done": False, "pending": True, "slow_down": True})
        if error in ("expired_token", "access_denied"):
            del _sessions[session_id]
            return JSONResponse({"success": False, "done": True, "error": f"Login failed: {error}"})
        if error:
            return JSONResponse({"success": False, "done": False, "error": error})

        access_token = data.get("access_token")
        if access_token:
            _save_copilot_token(access_token, credential_profile)
            del _sessions[session_id]
            logger.info("[OAuth/Copilot] Token received and saved")
            return JSONResponse({"success": True, "done": True})

        return JSONResponse({"success": False, "done": False, "pending": True})
    except Exception as e:
        logger.error("[OAuth/Copilot] Poll error: {}", e)
        return JSONResponse({"success": False, "done": False, "error": str(e)})


@router.post("/oauth/copilot/logout")
async def copilot_logout(request: Request) -> JSONResponse:
    """Delete cached Copilot token."""
    try:
        body = await _optional_json(request)
        profile_name = _validate_oauth_credential_profile(
            "github_copilot", body.get("credential_profile")
        )
        from nanobot.providers.oauth_profile_storage import require_provider_profile_wave

        require_provider_profile_wave("github_copilot", profile_name)
        _delete_copilot_token(profile_name)
        logger.info("[OAuth/Copilot] Token deleted")
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})
