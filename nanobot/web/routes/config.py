"""Config editor route — view and edit config.json."""

import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from loguru import logger

router = APIRouter()

_SEARCH_PROVIDERS = {"duckduckgo", "brave", "tavily", "searxng", "jina", "kagi"}


def _safe_restart_next_url(next_url: str) -> str:
    """Keep restart redirects inside the dashboard origin."""
    if not next_url or not next_url.startswith("/") or next_url.startswith("//"):
        return "/"
    return next_url


def _config_with_dashboard_section(
    config,
    *,
    model_presets: dict | None = None,
    provider_profiles: dict | None = None,
    fallback_models: list | None = None,
):
    """Return a validated Config with one dashboard JSON section replaced."""
    from nanobot.config.schema import Config, InlineFallbackConfig

    data = config.model_dump(mode="json", by_alias=True)
    if model_presets is not None:
        if not isinstance(model_presets, dict):
            raise ValueError("model_presets must be a JSON object")
        data["model_presets"] = model_presets
    if provider_profiles is not None:
        if not isinstance(provider_profiles, dict):
            raise ValueError("provider_profiles must be a JSON object")
        data["provider_profiles"] = provider_profiles
    if fallback_models is not None:
        if not isinstance(fallback_models, list):
            raise ValueError("fallback_models must be a JSON array")
        data["agents"]["defaults"]["fallback_models"] = [
            item if isinstance(item, str) else InlineFallbackConfig.model_validate(item).model_dump(mode="json", by_alias=True)
            for item in fallback_models
        ]
    updated = Config.model_validate(data)
    active_preset = updated.agents.defaults.model_preset
    if model_presets is not None and active_preset and active_preset not in updated.model_presets:
        updated.agents.defaults.model_preset = None
    return updated


def _json_error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"success": False, "error": message}, status_code=status_code)


def _wants_json_response(request: Request) -> bool:
    return (
        request.headers.get("x-requested-with", "").lower() == "xmlhttprequest"
        or "application/json" in request.headers.get("accept", "").lower()
    )


def _config_save_error(request: Request, message: str, *, redirect_error: str | None = None, status_code: int = 400):
    if _wants_json_response(request):
        return JSONResponse({"success": False, "error": message}, status_code=status_code)
    target = redirect_error or message.replace(" ", "%20")
    return RedirectResponse(url=f"/config?error={target}", status_code=302)

# ── Tested models cache ──────────────────────────────────


def _get_tested_models_path():
    """Get path to tested_models.json in user data dir."""
    from nanobot.config.paths import get_data_dir
    return get_data_dir() / "tested_models.json"


def _load_tested_models() -> dict:
    """Load tested models cache from disk."""
    path = _get_tested_models_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_tested_model(provider: str, model: str) -> None:
    """Save a tested model name for a provider."""
    models = _load_tested_models()
    if provider not in models:
        models[provider] = []
    if model not in models[provider]:
        models[provider].append(model)
    path = _get_tested_models_path()
    path.write_text(json.dumps(models, indent=2), encoding="utf-8")


@router.post("/config/test-provider")
async def test_provider(request: Request):
    """Test API key + model by sending a short prompt via the appropriate SDK."""
    from nanobot.providers.registry import find_by_name

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "key_valid": False, "error": "Invalid request body"})

    model = (body.get("model") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    api_base = (body.get("api_base") or "").strip() or None
    provider_name = (body.get("provider") or "").strip()
    credential_profile = (body.get("credential_profile") or "").strip() or None

    if not model:
        return JSONResponse({
            "success": False, "key_valid": False,
            "error": "Bạn chưa nhập model name. Hãy điền tên model trước khi test.",
        })

    oauth_providers = {"openai_codex", "github_copilot", "xai_oauth"}

    # OAuth providers: no API key needed — use their own token mechanism
    if provider_name in oauth_providers:
        return await _test_oauth_provider(model, provider_name, credential_profile)

    if not api_key:
        return JSONResponse({"success": False, "key_valid": False, "error": "API key is required"})

    logger.info("[Config Test] Testing provider={} model={}", provider_name, model)

    spec = find_by_name(provider_name) if provider_name else None
    backend = spec.backend if spec else "openai_compat"

    # Determine effective base URL: user override → spec default → None
    effective_base = api_base or (spec.default_api_base if spec else None) or None

    # === Anthropic (native Anthropic SDK) ==================================
    if backend == "anthropic":
        return await _test_anthropic_provider(model, api_key, api_base, provider_name)

    # === Azure OpenAI (AzureOpenAI SDK) =====================================
    if backend == "azure_openai":
        return await _test_azure_provider(model, api_key, api_base, provider_name)

    # === All OpenAI-compatible providers (AsyncOpenAI) =====================
    return await _test_openai_compat_provider(model, api_key, effective_base, provider_name)


async def _test_oauth_provider(
    model: str,
    provider_name: str,
    credential_profile: str | None = None,
) -> JSONResponse:
    """Test OAuth-based providers using their stored token."""
    try:
        if credential_profile:
            from nanobot.config.loader import load_config

            selection = load_config().resolve_provider_selection(
                model,
                provider_override=provider_name,
                credential_profile=credential_profile,
            )
            if selection.provider_name != provider_name:
                raise ValueError(
                    f"credential_profile '{credential_profile}' belongs to provider "
                    f"'{selection.provider_name}', not '{provider_name}'"
                )
        if provider_name == "openai_codex":
            from nanobot.providers.openai_codex_provider import OpenAICodexProvider
            provider = OpenAICodexProvider(
                default_model=model,
                credential_profile=credential_profile,
            )
        elif provider_name == "github_copilot":
            from nanobot.providers.github_copilot_provider import GitHubCopilotProvider
            provider = GitHubCopilotProvider(
                default_model=model,
                credential_profile=credential_profile,
            )
        elif provider_name == "xai_oauth":
            from nanobot.providers.xai_oauth_provider import XaiOAuthProvider
            provider = XaiOAuthProvider(
                default_model=model,
                credential_profile=credential_profile,
            )
        else:
            return JSONResponse({"success": False, "key_valid": False, "error": f"Unknown OAuth provider: {provider_name}"})

        response = await provider.chat(
            messages=[{"role": "user", "content": "Say hi in one word."}],
            model=model,
        )

        result_text = (response.content or "").strip()
        if getattr(response, "finish_reason", "") == "error" or result_text.lower().startswith("error"):
            logger.warning(
                "[Config Test] OAuth provider={} model={} failed response: {}",
                provider_name,
                model,
                result_text[:200],
            )
            return JSONResponse({
                "success": False,
                "key_valid": True,
                "error": result_text[:500] or "OAuth provider returned an error",
            })
        logger.info("[Config Test] OAuth provider={} model={} OK: {}", provider_name, model, result_text[:30])
        _save_tested_model(provider_name, model)

        return JSONResponse({
            "success": True,
            "key_valid": True,
            "message": f'OAuth OK \u2014 "{result_text[:80]}"',
        })
    except Exception as e:
        logger.warning("[Config Test] OAuth provider={} model={} failed: {}", provider_name, model, e)
        return JSONResponse({"success": False, "key_valid": False, "error": str(e)})



async def _test_openai_compat_provider(model: str, api_key: str, api_base: str | None, provider_name: str) -> JSONResponse:
    """Test any OpenAI-compatible provider (openai, deepseek, groq, gemini, custom, etc.)."""
    from openai import AsyncOpenAI

    base_url = api_base or None

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    if provider_name == "openrouter" or (base_url and "openrouter" in base_url.lower()):
        headers["HTTP-Referer"] = "https://github.com/HKUDS/nanobot"
        headers["X-OpenRouter-Title"] = "nanobot"

    client = AsyncOpenAI(api_key=api_key, base_url=base_url, default_headers=headers)

    try:
        await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=5,
            timeout=15,
        )
        if provider_name:
            _save_tested_model(provider_name, model)
        logger.info("[Config Test] OK provider={} model={}", provider_name, model)
        return JSONResponse({"success": True, "key_valid": True, "message": "✓ API key valid, model responds"})
    except Exception as e:
        return _parse_openai_error(e, model, provider_name, api_base or "provider endpoint")


async def _test_anthropic_provider(model: str, api_key: str, api_base: str | None, provider_name: str) -> JSONResponse:
    """Test Anthropic provider using native Anthropic SDK."""
    from anthropic import AsyncAnthropic

    client_kw = {"api_key": api_key}
    if api_base:
        client_kw["base_url"] = api_base
    client = AsyncAnthropic(**client_kw)

    # Strip anthropic/ prefix if present
    bare_model = model[len("anthropic/"):] if model.startswith("anthropic/") else model

    try:
        await client.messages.create(
            model=bare_model,
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=5,
        )
        if provider_name:
            _save_tested_model(provider_name, model)
        logger.info("[Config Test] OK provider={} model={}", provider_name, model)
        return JSONResponse({"success": True, "key_valid": True, "message": "✓ API key valid, model responds"})
    except Exception as e:
        return _parse_openai_error(e, model, provider_name, "api.anthropic.com")


async def _test_azure_provider(model: str, api_key: str, api_base: str | None, provider_name: str) -> JSONResponse:
    """Test Azure OpenAI provider using AzureOpenAI SDK."""
    from openai import AsyncAzureOpenAI

    if not api_base:
        return JSONResponse({
            "success": False, "key_valid": False,
            "error": "Azure OpenAI requires api_base (e.g. https://your-resource.openai.azure.com/)",
        })

    client = AsyncAzureOpenAI(api_key=api_key, azure_endpoint=api_base, api_version="2024-10-21")

    try:
        await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=5,
            timeout=15,
        )
        if provider_name:
            _save_tested_model(provider_name, model)
        logger.info("[Config Test] OK provider={} model={}", provider_name, model)
        return JSONResponse({"success": True, "key_valid": True, "message": "✓ API key valid, model responds"})
    except Exception as e:
        return _parse_openai_error(e, model, provider_name, api_base)


def _parse_openai_error(e: Exception, model: str, provider_name: str, endpoint: str) -> JSONResponse:
    """Shared error parser for OpenAI/Anthropic SDK exceptions."""
    err_msg = str(e)
    try:
        err_msg = err_msg.encode("ascii", errors="replace").decode("ascii")
    except Exception:
        err_msg = repr(e)

    if "401" in err_msg or "Unauthorized" in err_msg or "authentication_error" in err_msg:
        logger.warning("[Config Test] FAIL provider={} -- Auth error", provider_name)
        return JSONResponse({"success": False, "key_valid": False, "error": "Invalid API key (authentication failed)"})
    if "404" in err_msg or "not_found" in err_msg or "not found" in err_msg.lower():
        logger.warning("[Config Test] WARN provider={} model={} -- Not found", provider_name, model)
        return JSONResponse({"success": False, "key_valid": True, "error": f"API key valid, but model '{model}' not found"})
    if "connect" in err_msg.lower() or "connection" in err_msg.lower() or "refused" in err_msg.lower():
        logger.warning("[Config Test] FAIL provider={} -- Connection error", provider_name)
        return JSONResponse({"success": False, "key_valid": False, "error": f"Cannot connect to {endpoint} — check API base URL"})

    logger.error("[Config Test] FAIL provider={} -- Error: {}", provider_name, err_msg[:200])
    return JSONResponse({"success": False, "key_valid": False, "error": err_msg[:200]})


@router.get("/config/tested-models")
async def get_tested_models():
    """Return the cached tested models per provider."""
    models = _load_tested_models()
    return JSONResponse(models)


@router.delete("/config/tested-models")
async def delete_tested_model(request: Request):
    """Remove a specific model from a provider's tested list."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid request body"})

    provider = (body.get("provider") or "").strip()
    model = (body.get("model") or "").strip()
    if not provider or not model:
        return JSONResponse({"success": False, "error": "provider and model required"})

    models = _load_tested_models()
    if provider in models and model in models[provider]:
        models[provider].remove(model)
        if not models[provider]:
            del models[provider]
        path = _get_tested_models_path()
        path.write_text(json.dumps(models, indent=2), encoding="utf-8")
        logger.info("[Config] Removed tested model provider={} model={}", provider, model)
    return JSONResponse({"success": True})


@router.get("/config/effective-route")
async def get_effective_route():
    """Read-only view of the resolved model route. Never includes key material."""
    from nanobot.config.loader import load_config
    from nanobot.providers.registry import find_by_name

    try:
        config = load_config()
    except Exception as exc:
        return _json_error(f"Failed to load config: {exc}", status_code=500)

    warnings: list[str] = []
    defaults = config.agents.defaults

    active_preset = (defaults.model_preset or "").strip()
    if active_preset and active_preset not in config.model_presets:
        warnings.append(
            f"Active route '{active_preset}' is not defined — running Manual (agents.defaults) instead."
        )

    effective = config.resolve_effective_model_config()

    def _describe_credential(model: str, provider: str, credential_profile: str | None) -> dict:
        try:
            selection = config.resolve_provider_selection(
                model, provider_override=provider, credential_profile=credential_profile
            )
        except ValueError as exc:
            warnings.append(str(exc))
            return {"source": "invalid", "detail": str(exc)}
        if selection.credential_profile:
            return {
                "source": "profile",
                "name": selection.credential_profile,
                "provider": selection.provider_name,
            }
        if selection.provider_name is None:
            warnings.append(
                "No provider matched the current model — configure an API key under Accounts & Credentials."
            )
            return {"source": "none"}
        spec = find_by_name(selection.provider_name)
        if spec is not None and spec.is_oauth:
            return {"source": "oauth", "provider": selection.provider_name}
        has_key = bool(getattr(selection.provider_config, "api_key", "")) if selection.provider_config else False
        if not has_key and not (spec is not None and spec.is_local):
            warnings.append(f"Provider '{selection.provider_name}' has no API key configured.")
        return {"source": "global", "provider": selection.provider_name}

    route = {
        "mode": "preset" if effective.preset_name else "custom",
        "preset_name": effective.preset_name,
        "model": effective.model,
        "provider": effective.provider,
        "credential": _describe_credential(effective.model, effective.provider, effective.credential_profile),
        "temperature": effective.temperature,
        "max_tokens": effective.max_tokens,
        "context_window_tokens": effective.context_window_tokens,
        "reasoning_effort": effective.reasoning_effort,
    }

    def _identity(resolved) -> tuple:
        return (
            resolved.model,
            resolved.provider,
            resolved.credential_profile or "",
            resolved.temperature,
            resolved.max_tokens,
            resolved.context_window_tokens,
            resolved.reasoning_effort or "",
        )

    primary_identity = _identity(effective)
    fallback_chain = []
    for index, candidate in enumerate(defaults.fallback_models):
        if isinstance(candidate, str):
            entry = {"name": candidate, "kind": "preset"}
            resolved = config.model_presets.get(candidate)
            if resolved is None:
                entry["status"] = "missing_preset"
                warnings.append(
                    f"Fallback #{index + 1} route '{candidate}' is not defined — it will be skipped at runtime."
                )
                fallback_chain.append(entry)
                continue
        else:
            entry = {"name": f"inline #{index + 1}", "kind": "inline"}
            resolved = candidate
        entry["model"] = resolved.model
        entry["provider"] = resolved.provider
        if resolved.credential_profile:
            entry["credential_profile"] = resolved.credential_profile
        if _identity(resolved) == primary_identity:
            entry["status"] = "same_as_primary"
            warnings.append(
                f"Fallback #{index + 1} '{entry['name']}' matches the active route — it will be skipped at runtime."
            )
        else:
            entry["status"] = "ok"
        fallback_chain.append(entry)

    return JSONResponse({
        "success": True,
        "route": route,
        "fallback_chain": fallback_chain,
        "warnings": warnings,
    })


@router.post("/config/save-provider")
async def save_provider(request: Request):
    """Save a single provider's API key and base URL to config."""
    from nanobot.config.loader import load_config, save_config

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid request body"})

    provider_name = (body.get("provider") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    api_base = (body.get("api_base") or "").strip()

    if not provider_name:
        return JSONResponse({"success": False, "error": "Provider name is required"})

    config = load_config()
    p = getattr(config.providers, provider_name, None)
    if not p:
        return JSONResponse({"success": False, "error": f"Unknown provider: {provider_name}"})

    p.api_key = api_key if api_key else ""
    p.api_base = api_base if api_base else None
    save_config(config)
    # The dashboard's own copy follows, as in the other save routes: the chat page reads it to know
    # whether Groq has a key before it offers Groq voice recognition
    request.app.state.config = config

    logger.info("[Config] Saved provider={} key={}...{}", provider_name,
                api_key[:4] if len(api_key) > 4 else "****",
                api_key[-4:] if len(api_key) > 4 else "")
    return JSONResponse({"success": True, "message": f"Provider {provider_name} saved"})


@router.post("/config/save-search-provider")
async def save_search_provider(request: Request):
    """Save only the web search provider settings without reloading the dashboard."""
    from nanobot.config.loader import load_config, save_config

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid request body"}, status_code=400)

    provider = (body.get("provider") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    base_url = (body.get("base_url") or "").strip()

    if provider not in _SEARCH_PROVIDERS:
        return JSONResponse({"success": False, "error": "Unsupported search provider"}, status_code=400)

    config = load_config()
    search = config.tools.web.search
    search.provider = provider
    search.api_key = api_key if provider not in {"duckduckgo", "searxng"} else ""
    search.base_url = base_url if provider == "searxng" else ""

    save_config(config)
    request.app.state.config = config
    logger.info("[Config] Saved search provider={}", provider)
    return JSONResponse({"success": True, "message": f"Search provider {provider} saved"})


@router.post("/config/save-model")
async def save_model(request: Request):
    """Save model name (and optionally provider) to config."""
    from nanobot.config.loader import load_config, save_config

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid request body"})

    model = (body.get("model") or "").strip()
    provider = (body.get("provider") or "").strip()
    if not model:
        return JSONResponse({"success": False, "error": "Model name is required"})

    config = load_config()
    config.agents.defaults.model = model
    if provider:
        config.agents.defaults.provider = provider
    save_config(config)

    # Update in-memory config so agent loop uses new settings immediately
    request.app.state.config = config

    logger.info("[Config] Saved model={} provider={}", model, provider or "(unchanged)")
    return JSONResponse({"success": True, "message": f"Model '{model}' saved"})


@router.post("/config/model-presets")
async def save_model_presets(request: Request):
    """Save only the dashboard model_presets section."""
    from nanobot.config.loader import load_config, save_config

    try:
        body = await request.json()
    except Exception:
        return _json_error("Invalid request body")

    if "model_presets" not in body:
        return _json_error("model_presets is required")
    raw_presets = body.get("model_presets")
    raw_fallbacks = body.get("fallback_models") if "fallback_models" in body else None
    try:
        config = load_config()
        updated = _config_with_dashboard_section(
            config,
            model_presets=raw_presets,
            fallback_models=raw_fallbacks,
        )
    except Exception as exc:
        logger.warning("[Web Config] Invalid model presets AJAX save: {}", exc)
        return _json_error(f"Invalid model presets: {exc}")

    save_config(updated)
    request.app.state.config = updated
    logger.info("[Config] Saved {} model preset(s)", len(updated.model_presets))
    return JSONResponse({"success": True, "message": "Model presets saved"})


@router.post("/config/provider-profiles")
async def save_provider_profiles(request: Request):
    """Save only the dashboard provider_profiles section."""
    from nanobot.config.loader import load_config, save_config

    try:
        body = await request.json()
    except Exception:
        return _json_error("Invalid request body")

    if "provider_profiles" not in body:
        return _json_error("provider_profiles is required")
    raw_profiles = body.get("provider_profiles")
    try:
        config = load_config()
        updated = _config_with_dashboard_section(config, provider_profiles=raw_profiles)
    except Exception as exc:
        logger.warning("[Web Config] Invalid provider profiles AJAX save: {}", exc)
        return _json_error(f"Invalid provider profiles: {exc}")

    save_config(updated)
    request.app.state.config = updated
    logger.info("[Config] Saved {} credential profile(s)", len(updated.provider_profiles))
    return JSONResponse({"success": True, "message": "Credential profiles saved"})


@router.post("/config/toggle-channel")
async def toggle_channel(request: Request):
    """Enable or disable a channel."""
    from nanobot.config.loader import load_config, save_config

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid request body"})

    channel_name = (body.get("channel") or "").strip()
    enabled = bool(body.get("enabled", False))

    if not channel_name:
        return JSONResponse({"success": False, "error": "Channel name is required"})

    config = load_config()
    ch = getattr(config.channels, channel_name, None)

    if ch is None:
        # Channel not in config yet, initialize a dict for it
        if not enabled:
            return JSONResponse({"success": True, "message": f"Channel '{channel_name}' remains disabled"})
        setattr(config.channels, channel_name, {"enabled": True})
    elif isinstance(ch, dict):
        ch["enabled"] = enabled
    else:
        ch.enabled = enabled

    save_config(config)
    request.app.state.config = config

    action = "enabled" if enabled else "disabled"
    logger.info("[Config] Channel {} {}", channel_name, action)
    return JSONResponse({"success": True, "message": f"Channel '{channel_name}' {action}"})


@router.post("/config/check-path")
async def check_path(request: Request):
    """Check if a directory path is valid (syntax) or exists on disk."""
    import re
    from pathlib import Path
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"valid": False, "error": "Invalid JSON"}, status_code=400)
    path_str = (body.get("path") or "").strip()
    mode = body.get("mode", "exists")  # "syntax" or "exists"
    if not path_str:
        return JSONResponse({"valid": False, "error": "Đường dẫn trống"})
    # Syntax check: valid path format
    try:
        p = Path(path_str).expanduser().resolve()
        if not p.is_absolute():
            return JSONResponse({"valid": False, "error": "Đường dẫn không hợp lệ (không phải absolute path)"})
    except Exception:
        return JSONResponse({"valid": False, "error": "Cú pháp đường dẫn không hợp lệ"})
    if mode == "syntax":
        # Validate just the folder name portion (not the full resolved path)
        name = (body.get("name") or path_str).strip()
        if not re.match(r'^[a-zA-Z0-9_\-./\\]+$', name):
            return JSONResponse({"valid": False, "error": "Tên thư mục chứa ký tự không hợp lệ (chỉ cho phép chữ, số, -, _, .)" })
        if '//' in name or '\\\\' in name or name.startswith('.') or name.endswith('.'):
            return JSONResponse({"valid": False, "error": "Tên thư mục không hợp lệ"})
        return JSONResponse({"valid": True, "message": "Cú pháp hợp lệ", "exists": p.exists()})
    # Exists check
    if p.exists():
        if p.is_dir():
            return JSONResponse({"valid": True, "message": "Thư mục tồn tại"})
        else:
            return JSONResponse({"valid": False, "error": "Đường dẫn tồn tại nhưng không phải thư mục"})
    return JSONResponse({"valid": False, "error": "Thư mục không tồn tại"})


async def _test_search_api_key(provider: str, api_key: str) -> JSONResponse:
    """Test supported search provider API keys with a minimal request."""
    import httpx

    from nanobot.config.loader import load_config

    if not api_key:
        return JSONResponse({"success": False, "error": "API key is empty"})
    if provider not in {"brave", "tavily"}:
        return JSONResponse({"success": False, "error": "Search provider test is not supported"}, status_code=400)

    config = load_config()
    proxy = getattr(config.tools.web, "proxy", None) or None

    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=10.0) as client:
            if provider == "brave":
                r = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": "test", "count": 1},
                    headers={
                        "Accept": "application/json",
                        "X-Subscription-Token": api_key,
                    },
                )
            else:
                r = await client.post(
                    "https://api.tavily.com/search",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"query": "test", "max_results": 1},
                )
        if r.status_code == 200:
            data = r.json()
            results = data.get("web", {}).get("results", []) if provider == "brave" else data.get("results", [])
            return JSONResponse({
                "success": True,
                "message": f"Key hợp lệ! ({len(results)} result)",
            })
        elif r.status_code in (401, 403):
            return JSONResponse({"success": False, "error": f"Key không hợp lệ (HTTP {r.status_code})"})
        else:
            return JSONResponse({"success": False, "error": f"{provider} API trả về HTTP {r.status_code}"})
    except httpx.ProxyError as e:
        return JSONResponse({"success": False, "error": f"Proxy error: {e}"})
    except Exception as e:
        logger.error("[Config] Test search provider={} key failed: {}", provider, e)
        return JSONResponse({"success": False, "error": f"Connection error: {e}"})


@router.post("/config/test-search-provider")
async def test_search_provider(request: Request):
    """Test supported search provider API keys from the dashboard."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON"}, status_code=400)

    provider = (body.get("provider") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    return await _test_search_api_key(provider, api_key)


@router.post("/config/test-brave-key")
async def test_brave_key(request: Request):
    """Backward-compatible Brave-only search key test endpoint."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON"}, status_code=400)

    api_key = (body.get("api_key") or "").strip()
    return await _test_search_api_key("brave", api_key)


@router.post("/config/test-voice-key")
async def test_voice_key(request: Request):
    """Check a Groq key for voice recognition in Web Chat: Groq must accept it and offer the
    Whisper model. It asks Groq for its model list, which costs none of the key's speech quota."""
    import httpx

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON"}, status_code=400)
    api_key = (body.get("api_key") or "").strip() if isinstance(body, dict) else ""
    if not api_key:
        return JSONResponse({"success": False, "error": "Chưa nhập key"})

    config = request.app.state.config
    # Where the recordings go (providers.groq.apiBase, else Groq itself), without the transcription path
    base = re.sub(r"(/audio)?/transcriptions$", "", (config.providers.groq.api_base or "").rstrip("/"))
    base = base or "https://api.groq.com/openai/v1"
    model = (config.transcription.model if config.get_transcription_provider_name() == "groq" else None) \
        or "whisper-large-v3"
    try:
        async with httpx.AsyncClient(proxy=config.tools.web.proxy or None, timeout=10.0) as client:
            r = await client.get(f"{base}/models", headers={"Authorization": f"Bearer {api_key}"})
    except Exception as e:
        logger.error("[Config] Test Groq voice key failed: {}", e)
        return JSONResponse({"success": False, "error": f"Không kết nối được tới Groq: {e}"})
    if r.status_code in (401, 403):
        return JSONResponse({"success": False, "error": f"Key không hợp lệ (HTTP {r.status_code})"})
    if r.status_code != 200:
        return JSONResponse({"success": False, "error": f"Groq trả về HTTP {r.status_code}"})
    try:
        offered = {m.get("id") for m in r.json().get("data", []) if isinstance(m, dict)}
    except Exception:
        offered = set()
    if model not in offered:
        return JSONResponse({"success": False, "key_valid": True,
                             "error": f"Key hợp lệ, nhưng Groq không liệt kê model {model} cho key này"})
    return JSONResponse({"success": True, "message": f"Key hợp lệ, dùng được model {model}"})


@router.post("/config/test-telegram-token")
async def test_telegram_token(request: Request):
    """Test a Telegram bot token by calling getMe API."""
    import httpx

    from nanobot.config.loader import load_config

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid JSON"}, status_code=400)

    token = (body.get("token") or "").strip()
    if not token:
        return JSONResponse({"success": False, "error": "Token is empty"})

    # Use proxy from current config if available
    config = load_config()
    tg = getattr(config.channels, "telegram", None)
    proxy_url = tg.get("proxy") if isinstance(tg, dict) else getattr(tg, "proxy", None)

    try:
        async with httpx.AsyncClient(proxy=proxy_url or None, timeout=10.0) as client:
            r = await client.get(f"https://api.telegram.org/bot{token}/getMe")

        if r.status_code == 200:
            data = r.json()
            bot = data.get("result", {})
            bot_name = bot.get("first_name", "")
            username = bot.get("username", "")
            return JSONResponse({
                "success": True,
                "message": f"Token hợp lệ! Bot: {bot_name} (@{username})",
                "bot_name": bot_name,
                "username": username,
            })
        elif r.status_code == 401:
            return JSONResponse({"success": False, "error": "Token không hợp lệ (401 Unauthorized)"})
        else:
            return JSONResponse({"success": False, "error": f"Telegram API trả về HTTP {r.status_code}"})
    except httpx.ProxyError as e:
        return JSONResponse({"success": False, "error": f"Proxy error: {e}"})
    except Exception as e:
        logger.error("[Config] Test Telegram token failed: {}", e)
        return JSONResponse({"success": False, "error": f"Connection error: {e}"})

# Provider names in display order
_PROVIDER_NAMES = [
    "openai", "anthropic", "openrouter", "deepseek", "groq", "gemini", "xai", "xai_oauth",
    "custom", "azure_openai", "bedrock", "zhipu", "dashscope", "vllm", "ollama", "moonshot",
    "minimax", "mistral", "aihubmix", "siliconflow",
    "volcengine", "volcengine_coding_plan", "byteplus", "byteplus_coding_plan",
    "ovms", "openai_codex", "github_copilot",
]

_REASONING_EFFORT_OPTIONS = ["low", "medium", "high", "adaptive", "minimal", "none"]

# Channel names in display order
_CHANNEL_NAMES = [
    "telegram", "discord", "slack", "whatsapp", "feishu", "dingtalk",
    "mochat", "email", "qq", "matrix", "wecom", "weixin", "zalo",
]
# Channels offered in the "+ channels" dropdown: the ones this project supports today.
# Add a name here once another channel is supported. A channel already enabled in the
# config keeps its settings card either way.
_OFFERED_CHANNELS = ("telegram", "zalo")


def _tool_role_context(config) -> dict:
    """What the Tool_Role cards and the custom dialog show (agent/tool_roles.py)."""
    from nanobot.agent.tool_roles import (
        ROLE_ICONS,
        ROLES,
        TOOL_SECTIONS,
        mcp_server_visible,
        normalize_role,
        role_summary,
        role_tools,
    )

    tools_config = config.tools
    legacy = dict(
        enable_file_tools=tools_config.enable_file_tools,
        enable_web_tools=tools_config.enable_web_tools,
        enable_spawn=tools_config.enable_spawn,
        enable_cron=tools_config.enable_cron,
    )
    custom_selected = role_tools("custom", custom_tools=tools_config.custom_tools, **legacy)
    return {
        "tool_role": normalize_role(tools_config.tool_preset),
        "tool_roles": [
            {"id": role.id, "label": role.label, "description": role.description, "icon": ROLE_ICONS[role.id]}
            for role in ROLES.values()
        ],
        "tool_sections": [[title, note, [list(tool) for tool in tools]] for title, note, tools in TOOL_SECTIONS],
        "custom_selected": custom_selected,
        "custom_mcp_servers": [
            {"name": name, "visible": mcp_server_visible("custom", server), "enabled": server.enabled}
            for name, server in tools_config.mcp_servers.items()
        ],
        "tool_role_data": {
            "summaries": {role_id: role_summary(role_id) for role_id in ROLES},
            "tools": {role_id: role_tools(role_id) for role_id in ROLES if role_id != "custom"},
            "mcp": {role_id: "mcp" in role.groups for role_id, role in ROLES.items() if role_id != "custom"},
            "traderMode": bool(getattr(config, "trader_mode", False)),
        },
        "trader_mode": bool(getattr(config, "trader_mode", False)),
    }


@router.get("/config", response_class=HTMLResponse)
async def config_page(
    request: Request,
    saved: str | None = None,
    error: str | None = None,
):
    """Render the configuration editor page."""
    app_state = request.app.state
    config = app_state.config

    import json

    from nanobot.web.routes.chat import _voice_settings

    defaults = config.agents.defaults
    effective_model = config.resolve_effective_model_config()
    gateway = config.gateway
    heartbeat = gateway.heartbeat
    web_cfg = gateway.web

    # Providers data
    providers_data = []
    for name in _PROVIDER_NAMES:
        p = getattr(config.providers, name, None)
        if p:
            providers_data.append({
                "name": name,
                "api_key": p.api_key or "",
                "api_base": p.api_base or "",
                "region": getattr(p, "region", "") or "",
                "profile": getattr(p, "profile", "") or "",
                "has_key": bool(p.api_key),
            })

    # Channels data (only enabled)
    channels_data = []
    for name in _CHANNEL_NAMES:
        ch = getattr(config.channels, name, None)
        if ch and (ch.get("enabled", False) if isinstance(ch, dict) else getattr(ch, "enabled", False)):
            fields = {}
            if isinstance(ch, dict):
                for k, v in ch.items():
                    if k == "enabled":
                        continue
                    if isinstance(v, (str, int, float, bool)):
                        fields[k] = v
                    elif isinstance(v, list):
                        fields[k] = ", ".join(str(x) for x in v)
            else:
                for field_name in ch.model_fields:
                    if field_name in ("enabled",):
                        continue
                    val = getattr(ch, field_name)
                    if isinstance(val, (str, int, float, bool)):
                        fields[field_name] = val
                    elif isinstance(val, list):
                        fields[field_name] = ", ".join(str(v) for v in val)
            channels_data.append({"name": name, "fields": fields})

    # Disabled channels (for enable dropdown), limited to the supported ones (_OFFERED_CHANNELS)
    # Includes: channels with enabled=False AND channels not yet in config (ch=None)
    disabled_channels = []
    for name in _CHANNEL_NAMES:
        if name not in _OFFERED_CHANNELS:
            continue
        ch = getattr(config.channels, name, None)
        if ch is None:
            # Not yet configured → show in enable dropdown so user can set it up
            disabled_channels.append(name)
        elif not (ch.get("enabled", False) if isinstance(ch, dict) else getattr(ch, "enabled", False)):
            disabled_channels.append(name)

    # Tools data
    tools_config = config.tools
    channels_config = config.channels

    reasoning_effort = defaults.reasoning_effort or ""
    reasoning_effort_is_custom = bool(
        reasoning_effort and reasoning_effort not in _REASONING_EFFORT_OPTIONS
    )
    dream = defaults.dream
    provider_options = ["auto", *_PROVIDER_NAMES]

    context = {
        "request": request,
        # Agent defaults
        "model": defaults.model,
        "effective_model": effective_model.model,
        "effective_provider": effective_model.provider,
        "model_preset": defaults.model_preset or "",
        "model_preset_names": sorted(config.model_presets.keys()),
        "model_presets_json": json.dumps(
            {
                name: preset.model_dump(mode="json", by_alias=True)
                for name, preset in config.model_presets.items()
            },
            indent=2,
            ensure_ascii=False,
        ),
        "provider_profiles_json": json.dumps(
            {
                name: profile.model_dump(mode="json", by_alias=True)
                for name, profile in config.provider_profiles.items()
            },
            indent=2,
            ensure_ascii=False,
        ),
        "fallback_models_json": json.dumps(
            [
                item
                if isinstance(item, str)
                else item.model_dump(mode="json", by_alias=True)
                for item in defaults.fallback_models
            ],
            indent=2,
            ensure_ascii=False,
        ),
        "provider": defaults.provider,
        "temperature": defaults.temperature,
        "max_tokens": defaults.max_tokens,
        "max_tool_iterations": defaults.max_tool_iterations,
        "max_tool_result_chars": defaults.max_tool_result_chars,
        "context_window_tokens": defaults.context_window_tokens,
        "reasoning_effort": reasoning_effort,
        "unified_session": defaults.unified_session,
        "reasoning_effort_options": _REASONING_EFFORT_OPTIONS,
        "reasoning_effort_is_custom": reasoning_effort_is_custom,
        "reasoning_effort_custom_value": reasoning_effort if reasoning_effort_is_custom else "",
        "provider_options": provider_options,
        "dream_interval_h": dream.interval_h,
        "dream_model_override": dream.model_override or "",
        "dream_max_batch_size": dream.max_batch_size,
        "dream_max_iterations": dream.max_iterations,
        # Heartbeat
        "heartbeat_enabled": heartbeat.enabled,
        "heartbeat_interval": heartbeat.interval_s,
        # Web
        "web_port": web_cfg.port,
        "web_host": web_cfg.host,
        "web_password": web_cfg.password,
        # Voice in Web Chat: voice_recognition, voice_language, voice_groq_key
        **{f"voice_{name}": value for name, value in _voice_settings(config).items()},
        # Providers
        "providers": providers_data,
        "bedrock_region": getattr(config.providers.bedrock, "region", "") or "",
        "bedrock_profile": getattr(config.providers.bedrock, "profile", "") or "",
        # Channels
        "channels": channels_data,
        "disabled_channels": disabled_channels,
        "send_progress": channels_config.send_progress,
        "send_tool_hints": channels_config.send_tool_hints,
        "debounce_seconds": channels_config.debounce_seconds,
        # Tools
        "exec_timeout": tools_config.exec.timeout,
        "exec_path_append": tools_config.exec.path_append,
        "web_proxy": tools_config.web.proxy or "",
        "search_provider": tools_config.web.search.provider,
        "search_api_key": tools_config.web.search.api_key or "",
        "search_base_url": tools_config.web.search.base_url or "",
        "sandbox_mode": tools_config.sandbox_mode,
        "workspace": str(config.workspace_path),
        "workspace_subdir": tools_config.exec.workspace_subdir,
        "allowed_dirs": tools_config.exec.allowed_dirs,
        "tool_preset": tools_config.tool_preset,
        **_tool_role_context(config),
        "enable_file_tools": tools_config.enable_file_tools,
        "enable_web_tools": tools_config.enable_web_tools,
        "enable_spawn": tools_config.enable_spawn,
        "enable_cron": tools_config.enable_cron,
        "enable_mcp": tools_config.enable_mcp,
        "image_generation_enabled": tools_config.image_generation.enabled,
        "image_generation_provider": tools_config.image_generation.provider,
        "image_generation_model": tools_config.image_generation.model,
        "image_generation_default_aspect_ratio": tools_config.image_generation.default_aspect_ratio,
        "image_generation_default_image_size": tools_config.image_generation.default_image_size,
        "image_generation_max_images_per_turn": tools_config.image_generation.max_images_per_turn,
        "image_generation_save_dir": tools_config.image_generation.save_dir,
        # Flash
        "saved": saved == "1",
        "error": error,
    }

    return app_state.templates.TemplateResponse(request, "config.html", context)


@router.post("/config")
async def config_save(request: Request):
    """Save configuration changes."""
    from nanobot.config.loader import get_config_path, load_config, save_config
    from nanobot.config.schema import (
        Config,
        InlineFallbackConfig,
        ModelPresetConfig,
        ProviderProfileConfig,
    )

    form = await request.form()

    try:
        config_path = get_config_path()

        # Create backup before saving
        backup_path = config_path.with_suffix(".json.bak")
        if config_path.exists():
            import shutil
            shutil.copy2(config_path, backup_path)
            logger.info("[Web Config] Backup: {}", backup_path)

        # Reload config from disk
        config = load_config(config_path)

        # --- Agent defaults ---
        model_preset = form.get("model_preset")
        if model_preset is not None:
            config.agents.defaults.model_preset = model_preset.strip() or None

        unified_session = form.get("unified_session")
        if unified_session is not None:
            config.agents.defaults.unified_session = unified_session == "true"

        provider_profiles_json = form.get("provider_profiles_json")
        if provider_profiles_json is not None:
            try:
                raw_profiles = json.loads(provider_profiles_json.strip() or "{}")
                if not isinstance(raw_profiles, dict):
                    raise ValueError("provider_profiles_json must be a JSON object")
                config.provider_profiles = {
                    str(name): ProviderProfileConfig.model_validate(value)
                    for name, value in raw_profiles.items()
                }
            except Exception as exc:
                logger.warning("[Web Config] Invalid provider profiles JSON: {}", exc)
                return _config_save_error(
                    request,
                    "Invalid provider_profiles JSON",
                    redirect_error="Invalid%20provider_profiles%20JSON",
                )

        model_presets_json = form.get("model_presets_json")
        if model_presets_json is not None:
            try:
                raw_presets = json.loads(model_presets_json.strip() or "{}")
                if not isinstance(raw_presets, dict):
                    raise ValueError("model_presets_json must be a JSON object")
                config.model_presets = {
                    str(name): ModelPresetConfig.model_validate(value)
                    for name, value in raw_presets.items()
                }
            except Exception as exc:
                logger.warning("[Web Config] Invalid model presets JSON: {}", exc)
                return _config_save_error(
                    request,
                    "Invalid model_presets JSON",
                    redirect_error="Invalid%20model_presets%20JSON",
                )

        fallback_models_json = form.get("fallback_models_json")
        if fallback_models_json is not None:
            try:
                raw_fallbacks = json.loads(fallback_models_json.strip() or "[]")
                if not isinstance(raw_fallbacks, list):
                    raise ValueError("fallback_models_json must be a JSON array")
                config.agents.defaults.fallback_models = [
                    item if isinstance(item, str) else InlineFallbackConfig.model_validate(item)
                    for item in raw_fallbacks
                ]
            except Exception as exc:
                logger.warning("[Web Config] Invalid fallback models JSON: {}", exc)
                return _config_save_error(
                    request,
                    "Invalid fallback_models JSON",
                    redirect_error="Invalid%20fallback_models%20JSON",
                )

        field_map = {
            "model": (config.agents.defaults, "model", str),
            "provider": (config.agents.defaults, "provider", str),
            "temperature": (config.agents.defaults, "temperature", float),
            "max_tokens": (config.agents.defaults, "max_tokens", int),
            "max_tool_iterations": (config.agents.defaults, "max_tool_iterations", int),
            "max_tool_result_chars": (config.agents.defaults, "max_tool_result_chars", int),
            "context_window_tokens": (config.agents.defaults, "context_window_tokens", int),
            "reasoning_effort": (config.agents.defaults, "reasoning_effort", str),
        }

        for form_key, (obj, attr, typ) in field_map.items():
            if config.agents.defaults.model_preset and attr in {
                "model",
                "provider",
                "temperature",
                "max_tokens",
                "context_window_tokens",
                "reasoning_effort",
            }:
                continue
            value = form.get(form_key)
            if value is not None:
                value = value.strip()
                if not value:
                    continue  # skip empty values
                if typ is float:
                    parsed = float(value)
                elif typ is int:
                    parsed = int(value)
                elif attr == "reasoning_effort":
                    parsed = type(obj).normalize_reasoning_effort(value)
                else:
                    parsed = value if value else None
                if attr == "max_tool_result_chars" and not 1000 <= parsed <= 64000:
                    return _config_save_error(
                        request,
                        "Max Tool Result Chars must be between 1000 and 64000",
                        redirect_error="Invalid%20Max%20Tool%20Result%20Chars",
                    )
                setattr(obj, attr, parsed)

        dream = config.agents.defaults.dream
        dream_interval_h = form.get("dream_interval_h")
        if dream_interval_h:
            dream.interval_h = int(dream_interval_h)
        dream_model_override = form.get("dream_model_override")
        if dream_model_override is not None:
            dream.model_override = dream_model_override.strip() or None
        dream_max_batch_size = form.get("dream_max_batch_size")
        if dream_max_batch_size:
            dream.max_batch_size = int(dream_max_batch_size)
        dream_max_iterations = form.get("dream_max_iterations")
        if dream_max_iterations:
            dream.max_iterations = int(dream_max_iterations)

        try:
            Config.model_validate(config.model_dump(mode="json", by_alias=True))
        except Exception as exc:
            logger.warning("[Web Config] Invalid model preset configuration: {}", exc)
            return _config_save_error(
                request,
                "Invalid model preset configuration",
                redirect_error="Invalid%20model%20preset%20configuration",
            )

        # --- Heartbeat ---
        config.gateway.heartbeat.enabled = form.get("heartbeat_enabled") == "on"
        hb_interval = form.get("heartbeat_interval")
        if hb_interval:
            config.gateway.heartbeat.interval_s = int(hb_interval)

        # --- Web config ---
        web_port = form.get("web_port")
        if web_port:
            config.gateway.web.port = int(web_port)
        web_host = form.get("web_host", "").strip()
        if web_host in ("127.0.0.1", "0.0.0.0"):
            config.gateway.web.host = web_host
        web_password = form.get("web_password")
        if web_password is not None:
            config.gateway.web.password = web_password.strip()
        # Voice in Web Chat: how speech is recognised and in which language (web/routes/chat.py)
        if form.get("voice_recognition") in ("browser", "groq"):
            config.gateway.web.voice.recognition = form.get("voice_recognition")
        voice_language = (form.get("voice_language") or "").strip()
        if re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*", voice_language):  # a tag such as vi-VN
            config.gateway.web.voice.language = voice_language

        # --- Providers ---
        for name in _PROVIDER_NAMES:
            p = getattr(config.providers, name, None)
            if not p:
                continue
            key_val = form.get(f"provider_{name}_api_key")
            if key_val is not None:
                key_val = key_val.strip()
                p.api_key = key_val if key_val else ""
            base_val = form.get(f"provider_{name}_api_base")
            if base_val is not None:
                p.api_base = base_val.strip() or None
        config.providers.bedrock.region = form.get("provider_bedrock_region", "").strip() or None
        config.providers.bedrock.profile = form.get("provider_bedrock_profile", "").strip() or None

        # --- Tools ---
        exec_timeout = form.get("exec_timeout")
        if exec_timeout:
            config.tools.exec.timeout = int(exec_timeout)
        exec_path = form.get("exec_path_append")
        if exec_path is not None:
            config.tools.exec.path_append = exec_path.strip()
        web_proxy = form.get("web_proxy")
        if web_proxy is not None:
            config.tools.web.proxy = web_proxy.strip() or None
        search_key = form.get("search_api_key")
        if search_key is not None:
            search_key = search_key.strip()
            config.tools.web.search.api_key = search_key if search_key else ""
        search_provider = form.get("search_provider")
        if search_provider:
            search_provider = search_provider.strip()
            if search_provider:
                config.tools.web.search.provider = search_provider
        search_base_url = form.get("search_base_url")
        if search_base_url is not None:
            config.tools.web.search.base_url = search_base_url.strip() or ""
        config.tools.image_generation.enabled = form.get("image_generation_enabled") == "on"
        image_generation_provider = form.get("image_generation_provider")
        if image_generation_provider is not None:
            config.tools.image_generation.provider = image_generation_provider.strip() or "openrouter"
        image_generation_model = form.get("image_generation_model")
        if image_generation_model is not None:
            config.tools.image_generation.model = image_generation_model.strip() or "openai/gpt-5.4-image-2"
        image_generation_aspect_ratio = form.get("image_generation_default_aspect_ratio")
        if image_generation_aspect_ratio is not None:
            config.tools.image_generation.default_aspect_ratio = image_generation_aspect_ratio.strip() or "1:1"
        image_generation_size = form.get("image_generation_default_image_size")
        if image_generation_size is not None:
            config.tools.image_generation.default_image_size = image_generation_size.strip() or "1K"
        image_generation_max_images = form.get("image_generation_max_images_per_turn")
        if image_generation_max_images:
            config.tools.image_generation.max_images_per_turn = int(image_generation_max_images)
        image_generation_save_dir = form.get("image_generation_save_dir")
        if image_generation_save_dir is not None:
            config.tools.image_generation.save_dir = image_generation_save_dir.strip() or "generated"

        # Sandbox mode
        sandbox_mode = form.get("sandbox_mode", "unrestricted").strip()
        if sandbox_mode in ("unrestricted", "workspace", "disabled"):
            config.tools.sandbox_mode = sandbox_mode
        workspace_subdir = form.get("workspace_subdir")
        if workspace_subdir is not None:
            config.tools.exec.workspace_subdir = workspace_subdir.strip()
        # Auto-create sandbox/subdir if sandbox mode is on
        if sandbox_mode == "workspace":
            sandbox_dir = config.workspace_path / "sandbox"
            if workspace_subdir and workspace_subdir.strip():
                sandbox_dir = sandbox_dir / workspace_subdir.strip()
            sandbox_dir.mkdir(parents=True, exist_ok=True)
        # allowed_dirs: collect from form (allowed_dir_0, allowed_dir_1, ...)
        allowed_dirs = []
        for key in sorted(form.keys()):
            if key.startswith("allowed_dir_"):
                val = form.get(key, "").strip()
                if val:
                    allowed_dirs.append(val)
        config.tools.exec.allowed_dirs = allowed_dirs

        # Tool role (agent/tool_roles.py); "developer" is the all-on preset from before roles
        from nanobot.agent.tool_roles import ALL_TOOLS, GROUP_TOOLS, ROLES, set_custom_visibility

        tool_preset = form.get("tool_preset", "developer").strip()
        if tool_preset in ROLES or tool_preset == "developer":
            config.tools.tool_preset = tool_preset
        if form.get("custom_tools_present") == "1":
            # The custom dialog: tools picked one by one. "message" is always on; keeping it in the
            # list marks the list as chosen even when nothing else is picked.
            picked = [name for name in dict.fromkeys(form.getlist("custom_tool")) if name in ALL_TOOLS]
            config.tools.custom_tools = ["message", *(name for name in picked if name != "message")]
            chosen = set(picked)
            config.tools.enable_file_tools = bool(chosen & {*GROUP_TOOLS["file_read"], *GROUP_TOOLS["file_write"]})
            config.tools.enable_web_tools = bool(chosen & set(GROUP_TOOLS["web"]))
            config.tools.enable_spawn = "spawn" in chosen
            config.tools.enable_cron = "cron" in chosen
            config.tools.enable_mcp = form.get("enable_mcp") == "on"
            if form.get("custom_mcp_present") == "1":
                shown = set(form.getlist("custom_mcp"))
                for server_name, server in config.tools.mcp_servers.items():
                    set_custom_visibility(server, server_name in shown)
        else:  # a page from before roles: the five group switches
            config.tools.enable_file_tools = form.get("enable_file_tools") == "on"
            config.tools.enable_web_tools = form.get("enable_web_tools") == "on"
            config.tools.enable_spawn = form.get("enable_spawn") == "on"
            config.tools.enable_cron = form.get("enable_cron") == "on"
            config.tools.enable_mcp = form.get("enable_mcp") == "on"

        # --- Channels global ---
        config.channels.send_progress = form.get("send_progress") == "on"
        config.channels.send_tool_hints = form.get("send_tool_hints") == "on"
        debounce_val = form.get("debounce_seconds")
        if debounce_val is not None:
            config.channels.debounce_seconds = max(0.0, float(debounce_val.strip() or "2.0"))

        # --- Telegram channel ---
        tg = getattr(config.channels, "telegram", None)
        tg_enabled = tg.get("enabled", False) if isinstance(tg, dict) else getattr(tg, "enabled", False)
        if tg and tg_enabled:
            def _tgset(obj, key, val):
                if isinstance(obj, dict):
                    aliases = {
                        "allow_from": "allowFrom",
                        "reply_to_message": "replyToMessage",
                        "group_policy": "groupPolicy",
                    }
                    json_key = aliases.get(key, key)
                    obj[json_key] = val
                    if json_key != key:
                        obj.pop(key, None)
                else:
                    setattr(obj, key, val)
            tg_token = form.get("channel_telegram_token")
            if tg_token is not None:
                tg_token = tg_token.strip()
                if tg_token:
                    _tgset(tg, "token", tg_token)
            tg_allow = form.get("channel_telegram_allow_from")
            if tg_allow is not None:
                raw = [x.strip() for x in tg_allow.split(",") if x.strip()]
                _tgset(tg, "allow_from", raw)
            tg_proxy = form.get("channel_telegram_proxy")
            if tg_proxy is not None:
                _tgset(tg, "proxy", tg_proxy.strip() or None)
            reply_val = form.get("channel_telegram_reply_to_message") == "on"
            _tgset(tg, "reply_to_message", reply_val)
            _tgset(tg, "streaming", form.get("channel_telegram_streaming") == "on")
            tg_group_policy = form.get("channel_telegram_group_policy")
            if tg_group_policy in {"mention", "ambient", "open"}:
                _tgset(tg, "group_policy", tg_group_policy)
            logger.info("[Web Config] Telegram channel config updated")

        # Save to disk
        save_config(config, config_path)
        logger.info("[Web Config] Configuration saved successfully")

        # Update app state
        request.app.state.config = config

        if _wants_json_response(request):
            return JSONResponse({"success": True, "message": "Configuration saved successfully"})
        return RedirectResponse(url="/config?saved=1", status_code=302)

    except Exception as e:
        logger.error("[Web Config] Failed to save: {}", e)
        if _wants_json_response(request):
            return JSONResponse({"success": False, "error": "Failed to save configuration"}, status_code=500)
        return RedirectResponse(url="/config", status_code=302)


@router.post("/config/restart")
async def config_restart(request: Request):
    """Restart the gateway process in-place.

    Uses os.execv() to replace the current process with a new one,
    preserving the same command-line arguments. Works on Windows,
    Linux, and Docker. Uses `-m nanobot` for Windows compatibility
    (sys.argv[0] may not be a full path on Windows).
    """
    import os
    import sys

    logger.info("[Web Config] Gateway restart requested via web dashboard")

    async def _do_restart():
        import asyncio
        await asyncio.sleep(0.5)  # Let the response reach the client
        logger.info("[Web Config] Restarting via os.execv...")
        os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

    import asyncio
    asyncio.create_task(_do_restart())
    return JSONResponse({"success": True, "message": "Gateway is restarting..."})


@router.get("/restarting", response_class=HTMLResponse)
async def restarting_page(request: Request, next: str = "/"):
    """Show a browser-facing restart progress page."""
    # Sub-agents still running end with the process: the page asks first (custom)
    subagents = getattr(getattr(request.app.state, "agent", None), "subagents", None)
    running = subagents.running_labels() if hasattr(subagents, "running_labels") else []
    return request.app.state.templates.TemplateResponse(
        request,
        "restarting.html",
        {"next_url": _safe_restart_next_url(next), "running_subagents": running},
    )

