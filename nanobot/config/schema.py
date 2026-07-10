"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Any, ClassVar, Literal

from loguru import logger
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings

from nanobot.cron.types import CronSchedule


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ChannelsConfig(Base):
    """Configuration for chat channels.

    Built-in and plugin channel configs are stored as extra fields (dicts).
    Each channel parses its own config in __init__.
    Per-channel "streaming": true enables streaming output (requires send_delta impl).
    """

    model_config = ConfigDict(extra="allow")

    send_progress: bool = True  # stream agent's text progress to the channel
    send_tool_hints: bool = False  # stream tool-call hints (e.g. read_file("…"))
    debounce_seconds: float = 2.0  # Custom: batch rapid messages within this window (0 = off)
    send_max_retries: int = Field(default=3, ge=0, le=10)  # Max delivery attempts (initial send included)
    transcription_provider: str = "groq"  # Voice transcription backend: "groq" or "openai"
    msteams: Any = Field(default_factory=dict)  # MS Teams channel config (parsed by channel itself)


class DreamConfig(Base):
    """Dream memory consolidation configuration."""

    _HOUR_MS = 3_600_000

    interval_h: int = Field(default=2, ge=1)  # Every 2 hours by default
    cron: str | None = Field(default=None, exclude=True)  # Legacy compatibility override
    model_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices("modelOverride", "model", "model_override"),
    )  # Optional Dream-specific model override
    max_batch_size: int = Field(default=20, ge=1)  # Max history entries per run
    max_iterations: int = Field(default=10, ge=1)  # Max tool calls per Phase 2

    def build_schedule(self, timezone: str) -> CronSchedule:
        """Build the runtime schedule, preferring the legacy cron override if present."""
        if self.cron:
            return CronSchedule(kind="cron", expr=self.cron, tz=timezone)
        return CronSchedule(kind="every", every_ms=self.interval_h * self._HOUR_MS)

    def describe_schedule(self) -> str:
        """Return a human-readable summary for logs and startup output."""
        if self.cron:
            return f"cron {self.cron} (legacy)"
        hours = self.interval_h
        return f"every {hours}h"


class ModelPresetConfig(Base):
    """Named model/provider preset."""

    label: str | None = None
    model: str
    provider: str = "auto"
    credential_profile: str | None = Field(
        default=None,
        validation_alias=AliasChoices("credentialProfile", "credential_profile"),
    )
    max_tokens: int = 8192
    context_window_tokens: int = 65_536
    temperature: float = 0.1
    reasoning_effort: str | None = None

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _normalize_reasoning_effort(cls, value: Any) -> str | None:
        return AgentDefaults.normalize_reasoning_effort(value)


class InlineFallbackConfig(ModelPresetConfig):
    """Inline fallback model entry using the same shape as a preset."""


FallbackCandidate = str | InlineFallbackConfig


class ResolvedModelConfig(Base):
    """Effective model/provider/generation settings after preset resolution."""

    preset_name: str | None = None
    model: str
    provider: str
    credential_profile: str | None = None
    max_tokens: int
    context_window_tokens: int
    temperature: float
    reasoning_effort: str | None = None


class AgentDefaults(Base):
    """Default agent configuration."""

    _KNOWN_REASONING_EFFORTS: ClassVar[set[str]] = {
        "low",
        "medium",
        "high",
        "adaptive",
        "minimal",
        "minimum",
        "none",
    }
    _WARNED_REASONING_EFFORTS: ClassVar[set[str]] = set()

    workspace: str = "~/.nanobot/workspace"
    model: str = "anthropic/claude-opus-4-5"
    model_preset: str | None = Field(
        default=None,
        validation_alias=AliasChoices("modelPreset", "model_preset"),
    )
    provider: str = (
        "auto"  # Provider name (e.g. "anthropic", "openrouter") or "auto" for auto-detection
    )
    max_tokens: int = 8192
    context_window_tokens: int = 65_536
    context_block_limit: int | None = None  # Max context message blocks before snipping (None = unlimited)
    max_tool_result_chars: int = Field(
        default=16_000,
        ge=1_000,
        le=64_000,
    )  # Truncate persisted tool results to this length
    provider_retry_mode: Literal["standard", "persistent"] = "standard"  # LLM retry strategy
    temperature: float = 0.1
    max_tool_iterations: int = 80  # Increased from upstream default 200; 40 was old local default
    # Deprecated compatibility field: accepted from old configs but ignored at runtime.
    memory_window: int | None = Field(default=None, exclude=True)
    reasoning_effort: str | None = None  # low / medium / high / adaptive — enables LLM thinking mode
    fallback_models: list[FallbackCandidate] = Field(default_factory=list)
    timezone: str = "UTC"  # IANA timezone, e.g. "Asia/Ho_Chi_Minh", "America/New_York"
    unified_session: bool = False  # Share one session across all channels — KEEP FALSE for multi-customer use
    disabled_skills: list[str] = Field(default_factory=list)  # Skill names to exclude from loading
    # Per-line git-blame age annotation in Phase 1 prompt (e.g. `← 3d` suffix).
    # Set to False if a specific LLM reacts poorly to the suffix.
    annotate_line_ages: bool = True
    session_ttl_minutes: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("idleCompactAfterMinutes", "sessionTtlMinutes"),
        serialization_alias="idleCompactAfterMinutes",
    )  # Auto-compact idle threshold in minutes (0 = disabled)
    dream: DreamConfig = Field(default_factory=DreamConfig)

    @classmethod
    def normalize_reasoning_effort(cls, value: Any) -> str | None:
        """Normalize known values but keep unknown gateway-specific values."""
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if not normalized or normalized == "default":
            return None
        if normalized not in cls._KNOWN_REASONING_EFFORTS and normalized not in cls._WARNED_REASONING_EFFORTS:
            logger.warning("Unknown reasoning_effort {!r}; keeping raw value for gateway compatibility", normalized)
            cls._WARNED_REASONING_EFFORTS.add(normalized)
        return normalized

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _normalize_reasoning_effort(cls, value: Any) -> str | None:
        return cls.normalize_reasoning_effort(value)

    @property
    def should_warn_deprecated_memory_window(self) -> bool:
        """Return True when old memoryWindow is present without contextWindowTokens."""
        return self.memory_window is not None and "context_window_tokens" not in self.model_fields_set



class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)
    extra_body: dict[str, Any] | None = None  # Extra request fields for providers that support them


class BedrockProviderConfig(ProviderConfig):
    """AWS Bedrock Runtime provider configuration."""

    region: str | None = None  # Optional AWS region override
    profile: str | None = None  # Optional AWS shared config profile


class ProviderProfileConfig(ProviderConfig):
    """Named credential profile for preset-scoped provider credentials."""

    provider: str
    region: str | None = None
    profile: str | None = None


class ResolvedProviderSelection(Base):
    """Resolved provider config plus credential profile identity."""

    provider_config: ProviderConfig | ProviderProfileConfig | BedrockProviderConfig | None
    provider_name: str | None
    requested_provider: str
    credential_profile: str | None = None
    api_base: str | None = None


class ProvidersConfig(Base):
    """Configuration for LLM providers."""

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    azure_openai: ProviderConfig = Field(default_factory=ProviderConfig)  # Azure OpenAI (model = deployment name)
    bedrock: BedrockProviderConfig = Field(default_factory=BedrockProviderConfig)  # AWS Bedrock Converse
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    assemblyai: ProviderConfig = Field(default_factory=ProviderConfig)  # Future transcription provider parity
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)  # 阿里云通义千问
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    lm_studio: ProviderConfig = Field(default_factory=ProviderConfig)  # LM Studio local models
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)  # Ollama local models
    ovms: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenVINO Model Server (OVMS)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    xai: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax_anthropic: ProviderConfig = Field(default_factory=ProviderConfig)  # MiniMax Anthropic endpoint (thinking)
    mistral: ProviderConfig = Field(default_factory=ProviderConfig)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)  # SiliconFlow (硅基流动)
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine (火山引擎)
    volcengine_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine Coding Plan
    byteplus: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus (VolcEngine international)
    byteplus_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus Coding Plan
    qianfan: ProviderConfig = Field(default_factory=ProviderConfig)  # Qianfan (百度千帆)
    stepfun: ProviderConfig = Field(default_factory=ProviderConfig)  # Step Fun (阶跃星辰)
    xiaomi_mimo: ProviderConfig = Field(default_factory=ProviderConfig)  # Xiaomi MIMO (小米)
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # OpenAI Codex (OAuth)
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # Github Copilot (OAuth)
    xai_oauth: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # xAI OAuth (experimental, no key)



class TranscriptionConfig(Base):
    """Cross-channel audio transcription configuration."""

    enabled: bool = True
    provider: str | None = None  # None falls back to legacy channels.transcription_provider
    model: str | None = None
    language: str | None = None
    max_duration_sec: int = Field(default=120, ge=1)
    max_upload_mb: int = Field(default=25, ge=1)


class HeartbeatConfig(Base):
    """Heartbeat service configuration."""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 minutes
    keep_recent_messages: int = 8  # Number of recent messages to retain between heartbeat runs


class WebConfig(Base):
    """Web dashboard configuration."""

    enabled: bool = False
    port: int = 8899
    host: str = "127.0.0.1"  # Bind address; use 0.0.0.0 for fleet/remote access
    password: str = ""  # Dashboard login password (empty = no auth)


class ChatbotConfig(Base):
    """Settings for human-like chatbot behavior (multi-message reply)."""

    multi_message_enabled: bool = False   # Split response into multiple messages
    multi_message_delimiter: str = "---+---"  # Delimiter the LLM uses to split
    typing_delay_base_ms: int = 800       # Minimum delay between messages (ms)
    typing_delay_per_char_ms: float = 20  # Extra delay per character (simulate typing)
    typing_delay_max_ms: int = 3000       # Maximum delay cap (ms)
    max_splits: int = Field(default=3, ge=1, le=5)  # Max message parts (1-5)


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790
    bot_id: str = ""  # Unique bot ID for fleet management
    fleet_id: str = ""  # Fleet this bot belongs to (empty = unassigned, prevents double-registration)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    web: WebConfig = Field(default_factory=WebConfig)


class WebSearchConfig(Base):
    """Web search tool configuration."""

    provider: str = "duckduckgo"  # brave, tavily, duckduckgo, searxng, jina, kagi
    api_key: str = ""
    base_url: str = ""  # SearXNG base URL
    max_results: int = 5
    timeout: int = 30  # Wall-clock timeout (seconds) for search operations


class WebToolsConfig(Base):
    """Web tools configuration."""

    enable: bool = True
    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ExecToolConfig(Base):
    """Shell exec tool configuration."""

    enable: bool = True  # Set to false to disable exec tool entirely (overrides sandbox_mode/tool_preset)
    timeout: int = 60
    path_append: str = ""
    sandbox: str = ""  # sandbox backend: "" (none) or "bwrap" (Linux only)
    allowed_env_keys: list[str] = Field(default_factory=list)  # Env var names to pass through to subprocess (e.g. ["GOPATH", "JAVA_HOME"])
    workspace_subdir: str = ""  # Optional subfolder INSIDE workspace/sandbox/ (custom)
    allowed_dirs: list[str] = Field(default_factory=list)  # Whitelisted dirs outside workspace (custom)


class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    enabled: bool = True
    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # auto-detected if omitted
    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    url: str = ""  # HTTP/SSE: endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE: custom headers
    tool_timeout: int = 30  # seconds before a tool call is cancelled
    enabled_tools: list[str] = Field(default_factory=lambda: ["*"])  # Only register these tools; accepts raw MCP names or wrapped mcp_<server>_<tool> names; ["*"] = all tools; [] = no tools


class MCPPresetBundleConfig(Base):
    """Reusable bundle of one or more MCP server configurations."""

    label: str
    description: str = ""
    servers: dict[str, MCPServerConfig]


class MCPBundleInstallationConfig(Base):
    """Tracks which materialized MCP servers belong to an installed bundle."""

    preset_id: str
    server_names: list[str]
    enabled: bool = True


class MyToolConfig(Base):
    """Configuration for the 'my' runtime inspection tool."""

    enable: bool = True
    allow_set: bool = False  # let `my` modify loop state (read-only if False)


class ImageGenerationToolConfig(Base):
    """Configuration for the image generation tool."""

    enabled: bool = False
    provider: str = "openrouter"
    model: str = "openai/gpt-5.4-image-2"
    default_aspect_ratio: str = "1:1"
    default_image_size: str = "1K"
    max_images_per_turn: int = Field(default=4, ge=1, le=8)
    save_dir: str = "generated"


class ToolsConfig(Base):
    """Tools configuration."""

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    sandbox_mode: str = "workspace"  # "unrestricted" | "workspace" | "disabled" (custom)
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    mcp_custom_presets: dict[str, MCPPresetBundleConfig] = Field(default_factory=dict)
    mcp_preset_installations: dict[str, MCPBundleInstallationConfig] = Field(default_factory=dict)
    ssrf_whitelist: list[str] = Field(default_factory=list)  # CIDR ranges to exempt from SSRF blocking
    # Tool preset: "developer" (all on) | "chatbot" (safe only) | "custom" (per-group) (custom)
    tool_preset: str = "developer"
    enable_file_tools: bool = True   # ReadFile, WriteFile, EditFile, ListDir
    enable_web_tools: bool = True    # WebSearch, WebFetch
    enable_spawn: bool = True        # SpawnTool (subagent)
    enable_cron: bool = True         # CronTool
    enable_mcp: bool = True          # MCP servers
    disabled_skills: list[str] = Field(default_factory=list)  # Manually disabled skill names
    my: MyToolConfig = Field(default_factory=MyToolConfig)
    image_generation: ImageGenerationToolConfig = Field(default_factory=ImageGenerationToolConfig)

    @model_validator(mode="before")
    @classmethod
    def _migrate_restrict_to_workspace(cls, data: Any) -> Any:
        """Backward compat: convert old restrict_to_workspace bool to sandbox_mode."""
        if isinstance(data, dict):
            rtw = data.pop("restrict_to_workspace", None)
            if rtw is True and "sandbox_mode" not in data:
                data["sandbox_mode"] = "workspace"
        return data


class ApiConfig(Base):
    """OpenAI-compatible API server configuration."""

    host: str = "127.0.0.1"  # Safer default: local-only bind.
    port: int = 8900
    timeout: float = 120.0  # Per-request timeout in seconds.


class Config(BaseSettings):
    """Root configuration for nanobot."""

    _WARNED_MISSING_MODEL_PRESETS: ClassVar[set[str]] = set()

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    chatbot: ChatbotConfig = Field(default_factory=ChatbotConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    model_presets: dict[str, ModelPresetConfig] = Field(default_factory=dict)
    provider_profiles: dict[str, ProviderProfileConfig] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("providerProfiles", "provider_profiles"),
    )

    @model_validator(mode="after")
    def _validate_model_presets_and_provider_profiles(self) -> "Config":
        from nanobot.providers.registry import find_by_name

        if any(name.strip().lower() == "default" for name in self.model_presets):
            raise ValueError("model_presets key 'default' is reserved")
        for name, profile in self.provider_profiles.items():
            normalized_name = name.strip().lower()
            if normalized_name in {"default", "auto"}:
                raise ValueError(f"provider_profiles key '{name}' is reserved")
            spec = find_by_name(profile.provider)
            if spec is None:
                raise ValueError(f"provider profile '{name}' references unknown provider '{profile.provider}'")
            if spec.name == "bedrock":
                raise ValueError(
                    f"provider profile '{name}' uses unsupported provider '{spec.name}'"
                )
            profile.provider = spec.name
        for preset_name, preset in self.model_presets.items():
            self._validate_preset_credential_profile(preset_name, preset)
        for index, candidate in enumerate(self.agents.defaults.fallback_models):
            if isinstance(candidate, InlineFallbackConfig):
                self._validate_preset_credential_profile(f"fallback_models[{index}]", candidate)
        return self

    def _validate_preset_credential_profile(self, preset_name: str, preset: ModelPresetConfig) -> None:
        from nanobot.providers.registry import find_by_name

        profile_name = (preset.credential_profile or "").strip()
        if not profile_name:
            return
        profile = self.provider_profiles.get(profile_name)
        if profile is None:
            raise ValueError(f"preset '{preset_name}' references missing credential_profile '{profile_name}'")
        if preset.provider == "auto":
            raise ValueError(f"preset '{preset_name}' cannot use provider 'auto' with credential_profile")
        preset_spec = find_by_name(preset.provider)
        profile_spec = find_by_name(profile.provider)
        if preset_spec is None:
            raise ValueError(f"preset '{preset_name}' references unknown provider '{preset.provider}'")
        if profile_spec is None:
            raise ValueError(
                f"credential_profile '{profile_name}' references unknown provider '{profile.provider}'"
            )
        if preset_spec.name != profile_spec.name:
            raise ValueError(
                f"preset '{preset_name}' provider '{preset_spec.name}' does not match "
                f"credential_profile '{profile_name}' provider '{profile_spec.name}'"
            )

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    def get_transcription_provider_name(self) -> str:
        """Resolve transcription provider, preferring top-level config over legacy channels config."""
        provider = (self.transcription.provider or "").strip()
        if provider:
            return provider
        return str(getattr(self.channels, "transcription_provider", "groq") or "groq")

    def get_transcription_language(self) -> str | None:
        """Resolve transcription language, accepting legacy channels.transcription_language."""
        if self.transcription.language:
            return self.transcription.language
        legacy = getattr(self.channels, "transcription_language", None)
        if legacy is None and getattr(self.channels, "model_extra", None):
            legacy = (
                self.channels.model_extra.get("transcription_language")
                or self.channels.model_extra.get("transcriptionLanguage")
            )
        return str(legacy) if legacy else None

    def get_transcription_provider_config(self) -> ProviderConfig | None:
        """Return provider config for the resolved transcription backend."""
        provider = self.get_transcription_provider_name().replace("-", "_")
        cfg = getattr(self.providers, provider, None)
        return cfg if isinstance(cfg, ProviderConfig) else None

    def get_transcription_api_key(self) -> str:
        """Return API key for the resolved transcription backend."""
        cfg = self.get_transcription_provider_config()
        return cfg.api_key if cfg else ""

    def get_transcription_api_base(self) -> str:
        """Return API base for the resolved transcription backend."""
        cfg = self.get_transcription_provider_config()
        return cfg.api_base or "" if cfg else ""

    def resolve_model_preset(self, name: str | None = None) -> ModelPresetConfig | None:
        """Resolve a named model preset, warning and falling back when missing."""
        preset_name = name if name is not None else self.agents.defaults.model_preset
        if not preset_name:
            return None
        preset = self.model_presets.get(preset_name)
        if preset is None:
            if preset_name not in self._WARNED_MISSING_MODEL_PRESETS:
                logger.warning(
                    "Model preset {!r} is not defined; falling back to agents.defaults model/provider",
                    preset_name,
                )
                self._WARNED_MISSING_MODEL_PRESETS.add(preset_name)
            return None
        return preset

    def resolve_effective_model_config(self) -> ResolvedModelConfig:
        """Return the active model/provider/generation config after preset resolution."""
        defaults = self.agents.defaults
        preset_name = defaults.model_preset
        preset = self.resolve_model_preset(preset_name)
        if preset is None:
            return ResolvedModelConfig(
                preset_name=None,
                model=defaults.model,
                provider=defaults.provider,
                credential_profile=None,
                max_tokens=defaults.max_tokens,
                context_window_tokens=defaults.context_window_tokens,
                temperature=defaults.temperature,
                reasoning_effort=defaults.reasoning_effort,
            )
        return ResolvedModelConfig(
            preset_name=preset_name,
            model=preset.model,
            provider=preset.provider,
            credential_profile=preset.credential_profile,
            max_tokens=preset.max_tokens,
            context_window_tokens=preset.context_window_tokens,
            temperature=preset.temperature,
            reasoning_effort=preset.reasoning_effort,
        )

    def effective_model(self) -> str:
        """Return the model selected by the active preset or legacy defaults."""
        return self.resolve_effective_model_config().model

    def effective_provider(self) -> str:
        """Return the provider selected by the active preset or legacy defaults."""
        return self.resolve_effective_model_config().provider

    def _match_provider(
        self, model: str | None = None, *, provider_override: str | None = None
    ) -> tuple["ProviderConfig | None", str | None]:
        """Match provider config and its registry name. Returns (config, spec_name)."""
        from nanobot.providers.registry import PROVIDERS, find_by_name

        forced = provider_override if provider_override is not None else self.agents.defaults.provider
        if forced != "auto":
            spec = find_by_name(forced)
            if spec:
                p = getattr(self.providers, spec.name, None)
                return (p, spec.name) if p else (None, None)
            return None, None

        model_lower = (model or self.agents.defaults.model).lower()
        model_normalized = model_lower.replace("-", "_")
        model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
        normalized_prefix = model_prefix.replace("-", "_")

        def _kw_matches(kw: str) -> bool:
            kw = kw.lower()
            return kw in model_lower or kw.replace("-", "_") in model_normalized

        # Explicit provider prefix wins — prevents `github-copilot/...codex` matching openai_codex.
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and model_prefix and normalized_prefix == spec.name:
                if spec.is_oauth or spec.is_local or spec.is_direct or p.api_key:
                    return p, spec.name

        # Match by keyword (order follows PROVIDERS registry)
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and any(_kw_matches(kw) for kw in spec.keywords):
                if spec.is_oauth or spec.is_local or spec.is_direct or p.api_key:
                    return p, spec.name

        # Fallback: configured local providers can route models without
        # provider-specific keywords (for example plain "llama3.2" on Ollama).
        # Prefer providers whose detect_by_base_keyword matches the configured api_base
        # (e.g. Ollama's "11434" in "http://localhost:11434") over plain registry order.
        local_fallback: tuple[ProviderConfig, str] | None = None
        for spec in PROVIDERS:
            if not spec.is_local:
                continue
            p = getattr(self.providers, spec.name, None)
            if not (p and p.api_base):
                continue
            if spec.detect_by_base_keyword and spec.detect_by_base_keyword in p.api_base:
                return p, spec.name
            if local_fallback is None:
                local_fallback = (p, spec.name)
        if local_fallback:
            return local_fallback

        # Fallback: gateways first, then others (follows registry order)
        # OAuth providers are NOT valid fallbacks — they require explicit model selection
        for spec in PROVIDERS:
            if spec.is_oauth:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and p.api_key:
                return p, spec.name
        return None, None

    def get_provider(
        self,
        model: str | None = None,
        *,
        provider_override: str | None = None,
        credential_profile: str | None = None,
    ) -> ProviderConfig | None:
        """Get matched provider config (api_key, api_base, extra_headers). Falls back to first available."""
        return self.resolve_provider_selection(
            model,
            provider_override=provider_override,
            credential_profile=credential_profile,
        ).provider_config

    def get_provider_name(
        self,
        model: str | None = None,
        *,
        provider_override: str | None = None,
        credential_profile: str | None = None,
    ) -> str | None:
        """Get the registry name of the matched provider (e.g. "deepseek", "openrouter")."""
        return self.resolve_provider_selection(
            model,
            provider_override=provider_override,
            credential_profile=credential_profile,
        ).provider_name

    def get_api_key(
        self,
        model: str | None = None,
        *,
        provider_override: str | None = None,
        credential_profile: str | None = None,
    ) -> str | None:
        """Get API key for the given model. Falls back to first available key."""
        p = self.get_provider(
            model,
            provider_override=provider_override,
            credential_profile=credential_profile,
        )
        return p.api_key if p else None

    def get_api_base(
        self,
        model: str | None = None,
        *,
        provider_override: str | None = None,
        credential_profile: str | None = None,
    ) -> str | None:
        """Get API base URL for the given model. Applies default URLs for gateway/local providers."""
        return self.resolve_provider_selection(
            model,
            provider_override=provider_override,
            credential_profile=credential_profile,
        ).api_base

    def resolve_provider_selection(
        self,
        model: str | None = None,
        *,
        provider_override: str | None = None,
        credential_profile: str | None = None,
    ) -> ResolvedProviderSelection:
        """Resolve provider config, preserving credential profile identity when selected."""
        from nanobot.providers.registry import find_by_name

        requested_provider = provider_override if provider_override is not None else self.agents.defaults.provider
        profile_name = (credential_profile or "").strip()
        if profile_name:
            profile = self.provider_profiles.get(profile_name)
            if profile is None:
                raise ValueError(f"credential_profile '{profile_name}' is not defined")
            spec = find_by_name(profile.provider)
            if spec is None:
                raise ValueError(
                    f"credential_profile '{profile_name}' references unknown provider '{profile.provider}'"
                )
            api_base = profile.api_base
            if not api_base and (spec.is_gateway or spec.is_local) and spec.default_api_base:
                api_base = spec.default_api_base
            return ResolvedProviderSelection(
                provider_config=profile,
                provider_name=spec.name,
                requested_provider=requested_provider,
                credential_profile=profile_name,
                api_base=api_base,
            )

        p, name = self._match_provider(model, provider_override=provider_override)
        api_base = p.api_base if p and p.api_base else None
        if not api_base and name:
            spec = find_by_name(name)
            if spec and (spec.is_gateway or spec.is_local) and spec.default_api_base:
                api_base = spec.default_api_base
        return ResolvedProviderSelection(
            provider_config=p,
            provider_name=name,
            requested_provider=requested_provider,
            credential_profile=None,
            api_base=api_base,
        )

    model_config = ConfigDict(
        env_prefix="NANOBOT_",
        env_nested_delimiter="__",
        extra="allow",
    )
