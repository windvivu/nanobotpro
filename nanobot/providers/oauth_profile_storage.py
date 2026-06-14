"""Profile-scoped token storage helpers for OAuth providers."""

from __future__ import annotations

import hashlib
import re

from oauth_cli_kit.storage import FileTokenStorage

from nanobot.config.paths import get_data_dir

_PROFILE_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _legacy_token_filename(provider_name: str) -> str:
    if provider_name == "openai_codex":
        from oauth_cli_kit import OPENAI_CODEX_PROVIDER

        return OPENAI_CODEX_PROVIDER.token_filename
    filenames = {
        "github_copilot": "github-copilot.json",
        "xai_oauth": "xai-oauth.json",
    }
    try:
        return filenames[provider_name]
    except KeyError as exc:
        raise ValueError(f"Unsupported OAuth provider: {provider_name}") from exc


def get_oauth_token_filename(provider_name: str, credential_profile: str | None = None) -> str:
    """Return a deterministic, traversal-safe token filename."""
    legacy_filename = _legacy_token_filename(provider_name)
    profile_name = (credential_profile or "").strip()
    if not profile_name:
        return legacy_filename

    slug = _PROFILE_SLUG_RE.sub("-", profile_name.lower()).strip("-") or "profile"
    digest = hashlib.sha256(f"{provider_name}\0{profile_name}".encode()).hexdigest()[:12]
    stem, extension = legacy_filename.rsplit(".", 1)
    return f"{stem}--{slug}--{digest}.{extension}"


def get_oauth_token_storage(
    provider_name: str,
    credential_profile: str | None = None,
) -> FileTokenStorage:
    """Build token storage for the default account or a named profile."""
    profile_name = (credential_profile or "").strip()
    return FileTokenStorage(
        token_filename=get_oauth_token_filename(provider_name, profile_name),
        data_dir=get_data_dir(),
        import_codex_cli=provider_name == "openai_codex" and not profile_name,
    )


def require_provider_profile_wave(provider_name: str, credential_profile: str | None) -> None:
    """Fail loudly until a provider-specific OAuth profile wave is enabled."""
    enabled_providers = {"openai_codex", "github_copilot", "xai_oauth"}
    if credential_profile and provider_name not in enabled_providers:
        raise RuntimeError(
            f"OAuth credential profile '{credential_profile}' for provider '{provider_name}' "
            "is not enabled until its provider-specific Phase 2 wave is complete."
        )
