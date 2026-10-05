"""Fail-closed manifest promotion; no LLM or filesystem paths are trusted."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from nanobot.brain_memory.config import BrainMemoryConfig
from nanobot.brain_memory.errors import DisabledError, FormatError, ScopeError
from nanobot.brain_memory.paths import resolve_ref
from nanobot.brain_memory.schemas import Fact, FactDraft, Manifest, canonical_hash, content_hash
from nanobot.brain_memory.store import BrainStore

_SECRET_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
    ("credential_assignment", re.compile(
        r"(?i)\b(?:api[_ -]?key|password|passwd|secret|token)\s*[:=]\s*[^\s]{8,}"
    )),
)
_INJECTION_PATTERNS = (
    re.compile(r"(?i)\bignore\s+(?:all\s+)?previous\s+instructions\b"),
    re.compile(r"(?i)\b(?:system|developer)\s+message\s*[:：]"),
    re.compile(r"(?i)\b(?:reveal|print|show)\s+(?:the\s+)?(?:system|developer)\s+prompt\b"),
    re.compile(r"<\|(?:system|assistant|user|im_start|im_end)\|>", re.IGNORECASE),
)


@dataclass(frozen=True)
class ScanReport:
    secrets: tuple[str, ...] = ()
    injections: tuple[str, ...] = ()

    @property
    def safe(self) -> bool:
        return not self.secrets and not self.injections


def scan_text(*values: str) -> ScanReport:
    text = "\n".join(values)
    secrets = tuple(name for name, pattern in _SECRET_PATTERNS if pattern.search(text))
    injections = tuple(str(index) for index, pattern in enumerate(_INJECTION_PATTERNS, start=1) if pattern.search(text))
    return ScanReport(secrets, injections)


@dataclass(frozen=True)
class PromotionResult:
    manifest_hash: str
    status: str
    fact_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    created_fact_ids: tuple[str, ...] = ()
    promotion_sequence: int | None = None


def _manifest_hash(manifest: Manifest) -> str:
    payload = manifest.model_dump(mode="json")
    # Keep hashes compatible with manifests written before staged snapshot
    # content fields were added.
    if not manifest.content_ref:
        payload.pop("content_ref", None)
    if manifest.content_hash is None:
        payload.pop("content_hash", None)
    return canonical_hash(payload)


def _claim_key(draft: FactDraft) -> str:
    """Identify the durable claim independently of source provenance."""
    normalize = lambda value: " ".join(value.split()).casefold()
    return canonical_hash({
        "title": normalize(draft.title),
        "body": normalize(draft.body),
        "type": draft.type,
        "topic": normalize(draft.topic),
        "entities": sorted(normalize(value) for value in draft.entities),
        "supersedes": sorted(draft.supersedes),
    })


class FactPromoter:
    """Promote only validated, source-bound manifests into append-only facts."""

    def __init__(self, store: BrainStore, config: BrainMemoryConfig | None = None):
        self.store = store
        self.config = config or store.config

    def _manifest_record(self, manifest: Manifest, result: PromotionResult) -> bytes:
        record = {
            "schema_version": 1,
            "manifest_hash": result.manifest_hash,
            "status": result.status,
            "fact_ids": list(result.fact_ids),
            "reasons": list(result.reasons),
            "created_fact_ids": list(result.created_fact_ids),
            "promotion_sequence": result.promotion_sequence,
            "manifest": manifest.model_dump(mode="json"),
        }
        return (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")

    def _existing_result(self, manifest: Manifest, digest: str) -> PromotionResult | None:
        file_digest = digest.removeprefix("sha256:")
        for status in ("accepted", "rejected"):
            ref = f"manifests/{status}/{file_digest}.json"
            path = self.store.root / ref
            if not path.is_file():
                continue
            try:
                record = json.loads(self.store.read_bytes(ref).decode("utf-8"))
                return PromotionResult(
                    digest, str(record["status"]), tuple(record.get("fact_ids", ())),
                    tuple(record.get("reasons", ())),
                    tuple(record.get("created_fact_ids", ())),
                    record.get("promotion_sequence"),
                )
            except (UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise FormatError("Existing promotion record is invalid") from exc
        return None

    def _validate_source(self, manifest: Manifest) -> None:
        source_ref = manifest.content_ref or manifest.source.source_ref
        expected_hash = manifest.content_hash or manifest.source.source_hash
        source = resolve_ref(self.store.workspace, source_ref)
        if not source.is_file():
            raise ScopeError("Manifest source is not a regular file")
        with source.open("rb") as stream:
            raw = stream.read(self.config.max_source_bytes + 1)
        if len(raw) > self.config.max_source_bytes:
            raise FormatError("Manifest source exceeds byte budget")
        if content_hash(raw) != expected_hash:
            raise FormatError("Manifest source hash does not match current source")

    def _draft_report(self, draft: FactDraft, manifest: Manifest) -> ScanReport:
        if draft.source.source_ref != manifest.source.source_ref:
            raise FormatError("Fact source differs from manifest source")
        if draft.source.source_hash != manifest.source.source_hash:
            raise FormatError("Fact source hash differs from manifest source")
        values = [draft.title, draft.body, draft.topic, *draft.entities, *draft.tags]
        return scan_text(*values)

    def _active_claims(self) -> dict[str, str]:
        """Return active claim keys to fact IDs for cross-source deduplication."""
        facts_dir = self.store.root / "facts"
        if not facts_dir.is_dir():
            return {}
        claims: dict[str, str] = {}
        for path in sorted(facts_dir.glob("*.md")):
            if path.is_symlink():
                continue
            fact = self.store.read_fact(path.stem)
            if fact.status == "active":
                claims[_claim_key(fact)] = fact.id
        return claims

    def promote(self, manifest: Manifest) -> PromotionResult:
        if not self.config.enabled or not self.config.learning_enabled:
            raise DisabledError("Brain Memory learning is disabled")
        with self.store.locked():
            digest = _manifest_hash(manifest)
            self._validate_source(manifest)
            existing = self._existing_result(manifest, digest)
            if existing:
                return existing
            if not manifest.facts:
                result = PromotionResult(digest, "rejected", reasons=("empty_manifest",))
                self.store._write_locked(
                    f"manifests/rejected/{digest.removeprefix('sha256:')}.json",
                    self._manifest_record(manifest, result),
                )
                return result

            drafts: list[FactDraft] = []
            reasons: list[str] = []
            for draft in manifest.facts:
                report = self._draft_report(draft, manifest)
                if report.secrets:
                    reasons.extend(f"secret:{item}" for item in report.secrets)
                if report.injections:
                    reasons.extend(f"prompt_injection:{item}" for item in report.injections)
                if draft.confidence < self.config.quarantine_confidence_threshold:
                    reasons.append("low_confidence")
                drafts.append(draft)
            if reasons:
                # Do not partially accept a multi-fact manifest. The complete
                # proposal is retained for audit in the rejected/quarantined ledger.
                status = "quarantined" if reasons and all(reason == "low_confidence" for reason in reasons) else "rejected"
                result = PromotionResult(digest, status, reasons=tuple(sorted(set(reasons))))
                self.store._write_locked(
                    f"manifests/rejected/{digest.removeprefix('sha256:')}.json",
                    self._manifest_record(manifest, result),
                )
                return result

            invalid_supersedes = False
            for draft in drafts:
                for target_id in draft.supersedes:
                    try:
                        target = self.store.read_fact(target_id)
                    except (FormatError, OSError, ValueError):
                        invalid_supersedes = True
                        continue
                    if target.status != "active":
                        invalid_supersedes = True
            if invalid_supersedes:
                result = PromotionResult(digest, "rejected", reasons=("invalid_supersedes",))
                self.store._write_locked(
                    f"manifests/rejected/{digest.removeprefix('sha256:')}.json",
                    self._manifest_record(manifest, result),
                )
                return result

            existing_claims = self._active_claims()
            created_at = datetime.now(timezone.utc).isoformat()
            facts: list[Fact] = []
            fact_ids: list[str] = []
            for draft in drafts:
                existing_id = existing_claims.get(_claim_key(draft))
                if existing_id:
                    fact_ids.append(existing_id)
                    continue
                fact = Fact(
                    **draft.model_dump(),
                    id=draft.identity(),
                    status="active",
                    created_at=created_at,
                )
                facts.append(fact)
                fact_ids.append(fact.id)
                existing_claims[_claim_key(draft)] = fact.id
            sequence_path = ".migration/promotion-sequence.json"
            sequence = 0
            if self.store.path(sequence_path).exists():
                try:
                    sequence = int(json.loads(self.store.read_bytes(sequence_path).decode("utf-8")).get("value", 0))
                except (UnicodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
                    raise FormatError("Promotion sequence is invalid")
            sequence += 1
            self.store._write_locked(
                sequence_path,
                (json.dumps({"schema_version": 1, "value": sequence}) + "\n").encode("utf-8"),
                replace=True,
            )
            result = PromotionResult(
                digest, "accepted", tuple(fact_ids),
                created_fact_ids=tuple(fact.id for fact in facts),
                promotion_sequence=sequence,
            )
            for fact in facts:
                self.store._write_locked(
                    f"facts/{fact.id}.md",
                    self.store_fact_bytes(fact),
                )
            self.store._write_locked(
                f"manifests/accepted/{digest.removeprefix('sha256:')}.json",
                self._manifest_record(manifest, result),
            )
            return result

    @staticmethod
    def store_fact_bytes(fact: Fact) -> bytes:
        from nanobot.brain_memory.store import render_markdown

        return render_markdown(
            fact.model_dump(mode="json", exclude={"body"}), fact.body
        ).encode("utf-8")
