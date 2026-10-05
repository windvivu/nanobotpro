"""Versioned portable contracts. Stored facts are immutable records."""

import hashlib
import json
import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nanobot.brain_memory.paths import validate_ref

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
FactId = Annotated[str, Field(pattern=r"^fact-[0-9a-f]{64}$")]
Label = Annotated[str, Field(min_length=1, max_length=200)]


def content_hash(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def canonical_hash(value: object) -> str:
    return content_hash(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                   separators=(",", ":"), allow_nan=False).encode("utf-8"))


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    schema_version: Literal[1] = 1


class Citation(Record):
    source_ref: str
    source_hash: Digest
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)

    @field_validator("source_ref")
    @classmethod
    def relative_source(cls, value: str) -> str:
        return validate_ref(value)

    @model_validator(mode="after")
    def ordered_lines(self) -> "Citation":
        if self.line_end < self.line_start:
            raise ValueError("Citation line_end precedes line_start")
        return self


class FactDraft(Record):
    title: Label
    body: str = Field(min_length=1, max_length=32000)
    type: Literal["preference", "decision", "knowledge", "event"] = "knowledge"
    topic: Label = "general"
    entities: tuple[Label, ...] = Field(default=(), max_length=50)
    tags: tuple[Label, ...] = Field(default=(), max_length=50)
    confidence: float = Field(ge=0, le=1)
    source: Citation
    source_session: str = Field(default="", max_length=200)
    supersedes: tuple[FactId, ...] = Field(default=(), max_length=50)

    @field_validator("title", "body", "topic")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        value = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()
        if not value:
            raise ValueError("Text must not be blank")
        return value

    def identity(self) -> str:
        # Hash the full normalized claim + provenance, not just the source:
        # one conversation may legitimately yield several different facts.
        return "fact-" + canonical_hash(FactDraft.model_validate(
            {k: v for k, v in self.model_dump(mode="json").items() if k in FactDraft.model_fields}
        ).model_dump(mode="json"))[7:]


class Fact(FactDraft):
    id: FactId
    status: Literal["active", "superseded", "quarantined", "rejected"] = "quarantined"
    # Superseded is derived from newer accepted facts; old files never change.
    created_at: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def verify_identity(self) -> "Fact":
        if self.id != self.identity():
            raise ValueError("Fact ID does not match its contents and provenance")
        return self


class Manifest(Record):
    source: Citation
    facts: tuple[FactDraft, ...] = Field(default=(), max_length=32)
    content_ref: str = ""
    content_hash: Digest | None = None

    @field_validator("content_ref")
    @classmethod
    def relative_content_ref(cls, value: str) -> str:
        return validate_ref(value) if value else value


class ConversationEvent(Record):
    """A selected source reference; raw conversation text stays on disk."""

    event_id: str = Field(min_length=1, max_length=200)
    source: Citation
    session_id: str = Field(default="", max_length=200)
    selected: bool = True
    # Migration jobs cite the original source for provenance while reading an
    # immutable staged snapshot for the LLM. Normal events leave these empty.
    content_ref: str = ""
    content_hash: Digest | None = None
    migration_batch_id: str = Field(default="", max_length=200)

    @field_validator("content_ref")
    @classmethod
    def relative_content_ref(cls, value: str) -> str:
        return validate_ref(value) if value else value


class Chunk(Record):
    chunk_id: str
    fact_id: FactId
    text: str = Field(max_length=32000)
    citation: Citation


class RetrievedChunk(Chunk):
    score: float


class GraphNode(Record):
    id: str = Field(min_length=1, max_length=200)
    label: Label
    kind: Literal["fact", "topic", "entity"]
    citation: Citation | None = None


class GraphEdge(Record):
    source: str
    target: str
    kind: Literal["wikilink", "topic", "entity", "supersedes"]


class GraphSnapshot(Record):
    generation: str
    nodes: tuple[GraphNode, ...] = Field(default=(), max_length=5000)
    links: tuple[GraphEdge, ...] = Field(default=(), max_length=20000)
