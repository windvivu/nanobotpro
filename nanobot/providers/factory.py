"""Shared provider factory for CLI, SDK, and hot-reload paths."""

from __future__ import annotations

from nanobot.config.schema import Config
from nanobot.providers.base import GenerationSettings, LLMProvider
from nanobot.providers.registry import find_by_name


class ProviderFactoryError(ValueError):
    """Raised when config cannot build a provider."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        provider_name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider_name = provider_name


def _generation_from_config(config: Config) -> GenerationSettings:
    defaults = config.resolve_effective_model_config()
    return GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        reasoning_effort=defaults.reasoning_effort,
    )


def make_provider(config: Config, *, allow_no_key: bool = False) -> LLMProvider:
    """Create the configured LLM provider.

    This is the single source of truth for provider construction. CLI-specific
    messaging belongs in the CLI wrapper, not here.
    """
    effective = config.resolve_effective_model_config()
    model = effective.model
    selection = config.resolve_provider_selection(
        model,
        provider_override=effective.provider,
        credential_profile=effective.credential_profile,
    )
    provider_name = selection.provider_name
    p = selection.provider_config
    spec = find_by_name(provider_name) if provider_name else None
    backend = spec.backend if spec else "openai_compat"
    api_key = p.api_key if p else None
    api_base = selection.api_base or None

    if (spec and backend == "openai_codex") or model.startswith("openai-codex/"):
        from nanobot.providers.openai_codex_provider import OpenAICodexProvider

        provider: LLMProvider = OpenAICodexProvider(
            default_model=model,
            credential_profile=selection.credential_profile,
        )

    elif spec and backend == "github_copilot":
        from nanobot.providers.github_copilot_provider import GitHubCopilotProvider

        provider = GitHubCopilotProvider(
            default_model=model,
            credential_profile=selection.credential_profile,
        )

    elif spec and backend == "xai_oauth":
        # EXPERIMENTAL: xAI OAuth reuses the upstream Grok-CLI client_id; see provider warning.
        from nanobot.providers.xai_oauth_provider import XaiOAuthProvider

        provider = XaiOAuthProvider(
            default_model=model,
            api_base=api_base,
            credential_profile=selection.credential_profile,
        )

    elif spec and backend == "azure_openai":
        from nanobot.providers.azure_openai_provider import AzureOpenAIProvider

        if not p or not p.api_key or not p.api_base:
            raise ProviderFactoryError(
                "Azure OpenAI requires api_key and api_base in config.",
                code="missing_api_base" if p and p.api_key else "missing_api_key",
                provider_name=provider_name,
            )
        provider = AzureOpenAIProvider(
            api_key=p.api_key,
            api_base=p.api_base,
            default_model=model,
        )

    elif spec and backend == "bedrock":
        from nanobot.providers.bedrock_provider import BedrockProvider

        provider = BedrockProvider(
            api_key=api_key,
            api_base=p.api_base if p else None,
            default_model=model,
            region=getattr(p, "region", None) if p else None,
            profile=getattr(p, "profile", None) if p else None,
            extra_body=p.extra_body if p else None,
        )

    elif spec and backend == "anthropic":
        from nanobot.providers.anthropic_provider import AnthropicProvider

        if not api_key and not allow_no_key:
            raise ProviderFactoryError(
                f"No API key configured for provider '{provider_name}'.",
                code="missing_api_key",
                provider_name=provider_name,
            )
        provider = AnthropicProvider(
            api_key=api_key,
            api_base=api_base,
            default_model=model,
            extra_headers=p.extra_headers if p else None,
        )

    elif backend == "openai_compat":
        from nanobot.providers.openai_compat_provider import OpenAICompatProvider

        key_optional = bool(
            (spec and (spec.is_oauth or spec.is_local or spec.is_direct))
            or model.startswith("bedrock/")
        )
        if not api_key and not key_optional and not allow_no_key:
            raise ProviderFactoryError(
                f"No API key configured for provider '{provider_name}'.",
                code="missing_api_key",
                provider_name=provider_name,
            )
        provider = OpenAICompatProvider(
            api_key=api_key,
            api_base=api_base,
            default_model=model,
            extra_headers=p.extra_headers if p else None,
            spec=spec,
        )

    else:
        raise ProviderFactoryError(
            f"Unsupported provider backend '{backend}' for provider '{provider_name}'.",
            code="unsupported_provider",
            provider_name=provider_name,
        )

    provider.generation = _generation_from_config(config)
    return provider
