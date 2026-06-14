"""Provider runtime snapshot helpers for hot-reload comparisons."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nanobot.config.schema import Config
from nanobot.providers.registry import find_by_name


def _freeze_mapping(value: Any) -> tuple[tuple[str, Any], ...]:
    """Return a stable representation for config dict comparisons."""
    if not isinstance(value, dict) or not value:
        return ()
    return tuple(sorted((str(key), _freeze_value(item)) for key, item in value.items()))


def _freeze_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value


@dataclass(frozen=True)
class ProviderSnapshot:
    """Effective provider/model state used by AgentLoop hot-reload."""

    preset_name: str | None
    model: str
    provider_name: str | None
    requested_provider: str
    credential_profile: str | None
    backend: str | None
    api_key: str
    api_base: str | None
    extra_headers: tuple[tuple[str, Any], ...]
    extra_body: tuple[tuple[str, Any], ...]
    bedrock_region: str | None
    bedrock_profile: str | None
    temperature: float
    max_tokens: int
    reasoning_effort: str | None
    key_optional: bool

    def connection_key(self) -> tuple[Any, ...]:
        """Fields that require provider reconstruction when changed."""
        return (
            self.provider_name,
            self.backend,
            self.credential_profile,
            self.api_key,
            self.api_base,
            self.extra_headers,
            self.extra_body,
            self.bedrock_region,
            self.bedrock_profile,
        )

    def generation_key(self) -> tuple[Any, ...]:
        """Fields that can be updated on the existing provider."""
        return (self.temperature, self.max_tokens, self.reasoning_effort)

    def runtime_model_key(self) -> tuple[Any, ...]:
        """Fields that affect runtime model/preset state."""
        return (self.model, self.preset_name, self.requested_provider, self.credential_profile)

    def requires_provider_recreate(self, previous: "ProviderSnapshot | None") -> bool:
        return previous is not None and self.connection_key() != previous.connection_key()

    def requires_generation_update(self, previous: "ProviderSnapshot | None") -> bool:
        return previous is not None and self.generation_key() != previous.generation_key()

    def requires_model_sync(self, previous: "ProviderSnapshot | None") -> bool:
        return previous is not None and self.runtime_model_key() != previous.runtime_model_key()


def provider_snapshot_from_config(config: Config) -> ProviderSnapshot:
    """Resolve the effective provider/model config without constructing a provider."""
    effective = config.resolve_effective_model_config()
    if not isinstance(getattr(effective, "model", None), str):
        defaults = config.agents.defaults
        requested_provider = getattr(defaults, "provider", "auto")
        if not isinstance(requested_provider, str):
            requested_provider = "auto"
        effective = type(
            "_EffectiveModelFallback",
            (),
            {
                "preset_name": getattr(defaults, "model_preset", None),
                "model": defaults.model,
                "provider": requested_provider,
                "credential_profile": None,
                "temperature": getattr(defaults, "temperature", 0.1),
                "max_tokens": getattr(defaults, "max_tokens", 8192),
                "reasoning_effort": getattr(defaults, "reasoning_effort", None),
            },
        )()
    model = effective.model
    requested_provider = effective.provider
    if isinstance(config, Config):
        selection = config.resolve_provider_selection(
            model,
            provider_override=requested_provider,
            credential_profile=effective.credential_profile,
        )
        provider_name = selection.provider_name
        provider_config = selection.provider_config
        api_base = selection.api_base or None
        credential_profile = selection.credential_profile
    else:
        provider_name = config.get_provider_name(model, provider_override=requested_provider)
        provider_config = config.get_provider(model, provider_override=requested_provider)
        api_base = config.get_api_base(model, provider_override=requested_provider) or None
        credential_profile = getattr(effective, "credential_profile", None)
    spec = find_by_name(provider_name) if provider_name else None
    backend = spec.backend if spec else "openai_compat"
    key_optional = bool(
        (spec and (spec.is_oauth or spec.is_local or spec.is_direct))
        or model.startswith("bedrock/")
    )

    return ProviderSnapshot(
        preset_name=effective.preset_name,
        model=model,
        provider_name=provider_name,
        requested_provider=requested_provider,
        credential_profile=credential_profile,
        backend=backend,
        api_key=getattr(provider_config, "api_key", "") or "",
        api_base=api_base if isinstance(api_base, str) else None,
        extra_headers=_freeze_mapping(getattr(provider_config, "extra_headers", None)),
        extra_body=_freeze_mapping(getattr(provider_config, "extra_body", None)),
        bedrock_region=getattr(provider_config, "region", None) if provider_config else None,
        bedrock_profile=getattr(provider_config, "profile", None) if provider_config else None,
        temperature=effective.temperature,
        max_tokens=effective.max_tokens,
        reasoning_effort=effective.reasoning_effort,
        key_optional=key_optional,
    )
