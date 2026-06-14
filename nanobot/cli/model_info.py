"""Model information helpers for the onboard wizard.

Provides model context window lookup and autocomplete suggestions.
Uses a built-in static model map (no litellm dependency).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

# ---------------------------------------------------------------------------
# Static model context window map
# key: model name (or substring), value: max input tokens
# Covers the most common models; lookup uses substring matching as fallback.
# ---------------------------------------------------------------------------
_STATIC_CONTEXT_MAP: dict[str, int] = {
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    "o4-mini": 200_000,
    # Anthropic Claude
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-sonnet": 200_000,
    "claude-3-haiku": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-opus-4": 200_000,
    # DeepSeek
    "deepseek-chat": 64_000,
    "deepseek-coder": 64_000,
    "deepseek-r1": 64_000,
    # Google Gemini
    "gemini-2.0-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "gemini-1.5-pro": 1_048_576,
    "gemini-1.5-flash": 1_048_576,
    "gemini-1.0-pro": 30_720,
    "gemini-pro": 30_720,
    # Qwen / DashScope
    "qwen-long": 10_000_000,
    "qwen-max": 32_768,
    "qwen-plus": 131_072,
    "qwen-turbo": 1_000_000,
    "qwen2.5": 128_000,
    "qwen3": 128_000,
    # Moonshot / Kimi
    "kimi-k2": 128_000,
    "moonshot-v1-128k": 128_000,
    "moonshot-v1-32k": 32_000,
    "moonshot-v1-8k": 8_000,
    # Mistral
    "mistral-large": 128_000,
    "mistral-medium": 32_000,
    "mistral-small": 32_000,
    "codestral": 256_000,
    "mixtral-8x22b": 65_536,
    "mixtral-8x7b": 32_768,
    # Zhipu / GLM
    "glm-4": 128_000,
    "glm-4-long": 1_000_000,
    "glm-4-plus": 128_000,
    "glm-4-flash": 128_000,
    # MiniMax
    "abab6.5": 245_760,
    "abab6.5s": 245_760,
    # Groq (fast inference, same underlying models)
    "llama3-70b": 8_192,
    "llama3-8b": 8_192,
    "llama-3.1-70b": 131_072,
    "llama-3.1-8b": 131_072,
    "llama-3.3-70b": 131_072,
    "gemma2-9b": 8_192,
    # Ollama / local (very model-specific, list common ones)
    "llama3": 8_192,
    "llama3.1": 131_072,
    "llama3.2": 131_072,
    "phi3": 131_072,
    "phi4": 131_072,
    "codellama": 16_384,
    "deepseek-coder-v2": 128_000,
}

# Curated preset model suggestions per provider (shown in autocomplete)
_PROVIDER_PRESETS: dict[str, list[str]] = {
    "openai": [
        "gpt-4o", "gpt-4o-mini", "o3", "o3-mini", "o4-mini",
        "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo",
    ],
    "anthropic": [
        "claude-opus-4-20250514", "claude-sonnet-4-20250514",
        "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022",
        "claude-3-opus-20240229",
    ],
    "deepseek": [
        "deepseek-chat", "deepseek-reasoner",
    ],
    "gemini": [
        "gemini-2.5-pro-preview-05-06", "gemini-2.0-flash", "gemini-1.5-pro",
        "gemini-1.5-flash",
    ],
    "dashscope": [
        "qwen-max", "qwen-plus", "qwen-turbo", "qwen-long",
        "qwen3-235b-a22b", "qwen3-30b-a3b",
    ],
    "moonshot": [
        "kimi-k2-0711-preview", "moonshot-v1-128k", "moonshot-v1-32k", "moonshot-v1-8k",
    ],
    "mistral": [
        "mistral-large-latest", "mistral-small-latest", "codestral-latest",
        "mixtral-8x22b-instruct-v0.1",
    ],
    "zhipu": [
        "glm-4-plus", "glm-4-long", "glm-4-flash", "glm-4",
    ],
    "minimax": [
        "abab6.5s-chat", "abab6.5-chat",
    ],
    "groq": [
        "llama-3.3-70b-versatile", "llama-3.1-70b-versatile",
        "llama-3.1-8b-instant", "gemma2-9b-it",
    ],
    "openrouter": [
        "anthropic/claude-3-5-sonnet", "openai/gpt-4o", "google/gemini-2.0-flash",
        "deepseek/deepseek-chat",
    ],
    "aihubmix": [
        "claude-3-5-sonnet-20241022", "gpt-4o", "deepseek-chat",
    ],
    "siliconflow": [
        "deepseek-ai/DeepSeek-V3", "Qwen/Qwen3-235B-A22B",
    ],
    "volcengine": [
        "doubao-pro-32k", "doubao-lite-32k",
    ],
    "vllm": [
        "meta-llama/Llama-3.1-8B-Instruct",
    ],
    "ollama": [
        "llama3.3", "llama3.1", "phi4", "qwen3", "deepseek-coder-v2",
    ],
    "azure_openai": [
        "gpt-4o", "gpt-4o-mini", "gpt-4", "gpt-35-turbo",
    ],
    "custom": [],
}


def _normalize_model_name(model: str) -> str:
    """Normalize model name for comparison."""
    return model.lower().replace("-", "_").replace(".", "")


@lru_cache(maxsize=512)
def get_model_context_limit(model: str, provider: str = "auto") -> int | None:
    """Get the maximum input context tokens for a model.

    Uses the static context map. Falls back to tiktoken heuristics if known.

    Args:
        model: Model name in any common format
        provider: Provider name for display use only

    Returns:
        Maximum input tokens, or None if unknown
    """
    # Try exact match
    if model in _STATIC_CONTEXT_MAP:
        return _STATIC_CONTEXT_MAP[model]

    # Try base name (strip provider prefix like "anthropic/claude-3-5-sonnet")
    base = model.split("/")[-1] if "/" in model else model

    if base in _STATIC_CONTEXT_MAP:
        return _STATIC_CONTEXT_MAP[base]

    # Try substring matching — longest matching key wins
    base_lower = base.lower()
    best_key = ""
    best_val = None
    for key, val in _STATIC_CONTEXT_MAP.items():
        if key in base_lower and len(key) > len(best_key):
            best_key = key
            best_val = val

    if best_val is not None:
        return best_val

    # Try tiktoken for OpenAI models (already installed as openai dep)
    try:
        import tiktoken
        enc = tiktoken.encoding_for_model(base)
        # tiktoken doesn't tell us context limits directly, but knowing the
        # encoding lets us infer: cl100k_base → 128k for modern GPT-4 models
        name = enc.name
        if "cl100k" in name:
            return 128_000
        if "o200k" in name:
            return 200_000
    except Exception:
        pass

    return None


@lru_cache(maxsize=1)
def _get_provider_keywords() -> dict[str, list[str]]:
    """Build provider keywords mapping from nanobot's provider registry."""
    try:
        from nanobot.providers.registry import PROVIDERS
        return {spec.name: list(spec.keywords) for spec in PROVIDERS if spec.keywords}
    except ImportError:
        return {}


def get_all_models() -> list[str]:
    """Return a combined list of all known model names (presets + static map)."""
    models: set[str] = set(_STATIC_CONTEXT_MAP.keys())
    for preset_list in _PROVIDER_PRESETS.values():
        models.update(preset_list)
    return sorted(models)


def get_model_suggestions(partial: str, provider: str = "auto", limit: int = 20) -> list[str]:
    """Get autocomplete suggestions for model names.

    Searches provider presets first (highest quality), then falls back to the
    full static model map for partial-string matches.

    Args:
        partial: Partial model name typed by user
        provider: Provider name for filtering (e.g., "openrouter", "minimax")
        limit: Maximum number of suggestions to return

    Returns:
        List of matching model names
    """
    partial_lower = partial.lower()

    # Priority 1 — provider-specific presets
    presets = _PROVIDER_PRESETS.get(provider, []) if provider and provider != "auto" else []
    if presets:
        if not partial:
            return presets[:limit]
        preset_matches = [m for m in presets if partial_lower in m.lower()]
        if preset_matches:
            return preset_matches[:limit]

    # Priority 2 — full model list with scoring
    all_models = get_all_models()
    if not all_models:
        return []

    # Apply provider keyword filter
    provider_keywords = _get_provider_keywords()
    allowed_kws = provider_keywords.get(provider.lower()) if provider and provider != "auto" else None

    matches: list[tuple[int, str]] = []
    for model in all_models:
        model_lower = model.lower()

        if allowed_kws and not any(kw in model_lower for kw in allowed_kws):
            continue

        if not partial:
            matches.append((0, model))
            continue

        if partial_lower in model_lower:
            pos = model_lower.find(partial_lower)
            matches.append((100 - pos, model))
        elif _normalize_model_name(partial) in _normalize_model_name(model):
            matches.append((50, model))

    matches.sort(key=lambda x: (-x[0], x[1]))
    return [m for _, m in matches][:limit]


def find_model_info(model_name: str) -> dict[str, Any] | None:
    """Find model info dict (context window only — cost data removed with litellm)."""
    ctx = get_model_context_limit(model_name)
    if ctx is None:
        return None
    return {"max_input_tokens": ctx}


def format_token_count(tokens: int) -> str:
    """Format token count for display (e.g., 200000 -> '200,000')."""
    return f"{tokens:,}"
