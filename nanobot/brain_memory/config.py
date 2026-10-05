"""Config contracts only; runtime services are created explicitly by adapters."""

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from nanobot.brain_memory.paths import validate_ref


class BrainMemoryConfig(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    enabled: bool = False
    retrieval_enabled: bool = False
    learning_enabled: bool = False
    # Retained for compatibility with earlier pilot configs. Dashboard viewing
    # is always available and does not enable bot retrieval or learning.
    graph_enabled: bool = True
    retrieval_sessions: list[str] | None = Field(default=None, max_length=500)
    learning_sessions: list[str] = Field(default_factory=list, max_length=500)
    migration_enabled: bool = False
    migration_sources: list[str] = Field(default_factory=list, max_length=500)
    migration_stage_only: bool = True
    migration_delete_sources: bool = False
    root: str = "memory/brain"
    # Learning uses the bot's resolved provider/model, per user decision.
    learning_daily_job_limit: int = Field(default=25, ge=1, le=50)
    # Maximum UTF-8 bytes sent to the model in one learning job. Larger
    # migration snapshots are split at text boundaries into multiple jobs.
    learning_input_bytes: int = Field(default=32_768, ge=16_384, le=131_072)
    retrieval_limit: int = Field(default=8, ge=1, le=50)
    # A direct FTS hit may pull in a small number of active facts that share
    # its topic/entity/wikilink relationships.  Keep this bounded so graph
    # navigation never turns into an unbounded context expansion.
    retrieval_relation_limit: int = Field(default=4, ge=0, le=20)
    retrieval_relation_hops: int = Field(default=1, ge=0, le=1)
    retrieval_token_budget: int = Field(default=4000, ge=128, le=16000)
    max_query_chars: int = Field(default=2000, ge=1, le=8000)
    max_source_bytes: int = Field(default=2_000_000, ge=1024, le=10_000_000)
    max_document_bytes: int = Field(default=4_000_000, ge=1024, le=20_000_000)
    max_fact_chars: int = Field(default=8000, ge=256, le=32000)
    quarantine_confidence_threshold: float = Field(default=0.65, ge=0, le=1)
    queue_capacity: int = Field(default=100, ge=1, le=1000)
    learning_timeout_seconds: int = Field(default=60, ge=1, le=300)
    learning_max_attempts: int = Field(default=3, ge=1, le=5)
    shutdown_timeout_seconds: int = Field(default=5, ge=1, le=30)
    graph_max_nodes: int = Field(default=500, ge=1, le=5000)
    graph_max_edges: int = Field(default=2000, ge=1, le=20000)
    max_response_bytes: int = Field(default=1_000_000, ge=1024, le=5_000_000)
    lock_timeout_seconds: int = Field(default=5, ge=1, le=30)

    def reads_session(self, session_id: str | None) -> bool:
        return self.enabled and self.retrieval_enabled and (
            self.retrieval_sessions is None or session_id in self.retrieval_sessions
        )

    def learns_session(self, session_id: str) -> bool:
        return self.enabled and self.learning_enabled and session_id in self.learning_sessions

    @field_validator("retrieval_sessions", "learning_sessions")
    @classmethod
    def session_keys(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        if any(not v or len(v) > 512 or any(ord(c) < 32 for c in v) for v in values):
            raise ValueError("Invalid session key")
        return list(dict.fromkeys(values))

    @field_validator("migration_sources")
    @classmethod
    def migration_refs(cls, values: list[str]) -> list[str]:
        for value in values:
            validate_ref(value)
        return list(dict.fromkeys(values))

    @field_validator("root")
    @classmethod
    def relative_root(cls, value: str) -> str:
        return validate_ref(value)
