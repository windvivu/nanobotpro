"""Bounded, durable learning worker. It never starts implicitly or sends data itself."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from loguru import logger

from nanobot.brain_memory.config import BrainMemoryConfig
from nanobot.brain_memory.errors import DisabledError, FormatError
from nanobot.brain_memory.promoter import FactPromoter, PromotionResult
from nanobot.brain_memory.schemas import ConversationEvent, Manifest, canonical_hash
from nanobot.brain_memory.store import BrainStore

ManifestFactory = Callable[[ConversationEvent], Awaitable[Manifest] | Manifest]


@dataclass(frozen=True)
class LearningResult:
    job_id: str
    status: str
    attempts: int
    promotion: PromotionResult | None = None
    error_type: str = ""
    duration_ms: int = 0


class LearningWorker:
    """A caller-owned worker for selected events.

    Queue persistence records only source references and hashes. The callback is
    injected by the runtime, so this module cannot accidentally select a model,
    call an LLM, or enqueue every conversation.
    """

    def __init__(
        self,
        store: BrainStore,
        promoter: FactPromoter,
        manifest_factory: ManifestFactory,
        config: BrainMemoryConfig | None = None,
        on_result: Callable[[LearningResult], None] | None = None,
    ):
        self.store = store
        self.promoter = promoter
        self.factory = manifest_factory
        self.config = config or store.config
        self.on_result = on_result
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self.config.queue_capacity)
        self._queued_ids: set[str] = set()
        self._processing_ids: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    @staticmethod
    def job_id(event: ConversationEvent) -> str:
        return "learn-" + canonical_hash(event.model_dump(mode="json"))[7:]

    def _job_ref(self, job_id: str) -> str:
        return f"manifests/pending/jobs/{job_id}.json"

    @staticmethod
    def _today() -> str:
        return datetime.now().astimezone().date().isoformat()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _job_record(self, event: ConversationEvent, job_id: str, *, attempts: int = 0,
                    status: str = "pending", error_type: str = "",
                    created_at: str | None = None, quota_date: str | None = None,
                    phase: str = "queued", fact_ids: tuple[str, ...] = (),
                    created_fact_ids: tuple[str, ...] = (),
                    promotion_sequence: int | None = None,
                    phase_history: list[dict] | None = None,
                    started_at: str | None = None,
                    last_finished_at: str | None = None,
                    last_duration_ms: int = 0,
                    total_duration_ms: int = 0) -> dict:
        now = self._now()
        return {
            "schema_version": 1,
            "job_id": job_id,
            "event": event.model_dump(mode="json"),
            "attempts": attempts,
            "status": status,
            "error_type": error_type,
            "created_at": created_at or self._now(),
            "updated_at": now,
            "quota_date": quota_date,
            "phase": phase,
            "fact_ids": list(fact_ids),
            "created_fact_ids": list(created_fact_ids),
            "promotion_sequence": promotion_sequence,
            "started_at": started_at,
            "last_finished_at": last_finished_at,
            "last_duration_ms": last_duration_ms,
            "total_duration_ms": total_duration_ms,
            "phase_history": phase_history if phase_history is not None else [{"phase": phase, "at": now}],
        }

    def _write_job(self, record: dict, *, replace: bool = True) -> None:
        raw = (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        with self.store.locked():
            self.store._write_locked(self._job_ref(record["job_id"]), raw, replace=replace)

    def _read_job(self, job_id: str) -> dict:
        try:
            record = json.loads(self.store.read_bytes(self._job_ref(job_id)).decode("utf-8"))
            if not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("job_id") != job_id:
                raise ValueError("unsupported job")
            ConversationEvent.model_validate(record["event"])
            if record.get("status") not in {"pending", "retry", "accepted", "rejected", "quarantined", "failed", "skipped"}:
                raise ValueError("unsupported status")
            if type(record.get("attempts")) is not int or record["attempts"] < 0:
                raise ValueError("invalid attempts")
            return record
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise FormatError("Learning job is invalid") from exc

    def _quarantine_job(self, job_id: str) -> None:
        """Preserve bad bytes for recovery, without blocking healthy siblings."""
        try:
            with self.store.locked():
                path = self.store.path(self._job_ref(job_id))
                if path.is_file():
                    from nanobot.brain_memory.schemas import content_hash
                    raw = self.store.read_bytes(self._job_ref(job_id))
                    self.store._write_locked(f"manifests/corrupt/{job_id}-{content_hash(raw)[7:]}.json", raw)
                    path.unlink()
        except (OSError, ValueError):
            logger.warning("Brain job quarantine unavailable: {}", job_id)

    def _daily_reserved_jobs(self) -> int:
        """Count logical jobs that have consumed today's learning quota."""
        jobs_dir = self.store.root / "manifests" / "pending" / "jobs"
        if not jobs_dir.is_dir():
            return 0
        today = self._today()
        count = 0
        for path in jobs_dir.glob("learn-*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("quota_date") == today:
                    count += 1
            except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
                continue
        return count

    def _reserve_daily_quota(self, record: dict) -> bool:
        """Reserve one logical job for today, once, before queueing it."""
        today = self._today()
        if record.get("quota_date") == today:
            return True
        if self._daily_reserved_jobs() >= self.config.learning_daily_job_limit:
            return False
        record["quota_date"] = today
        return True

    def _queue_job(self, job_id: str) -> bool:
        if job_id in self._queued_ids:
            return True
        try:
            self._queue.put_nowait(job_id)
        except asyncio.QueueFull:
            return False
        self._queued_ids.add(job_id)
        return True

    async def enqueue(self, event: ConversationEvent) -> str:
        if self._stopping or not self.config.enabled or not self.config.learning_enabled:
            raise DisabledError("Brain Memory learning is disabled")
        if not event.selected:
            raise ValueError("Only explicitly selected conversation events may be queued")
        job_id = self.job_id(event)
        ref = self._job_ref(job_id)
        exists = (self.store.root / ref).is_file()
        record = self._read_job(job_id) if exists else self._job_record(event, job_id)
        if record.get("status") not in {"pending", "retry"}:
            # An explicit migration action may retry a failed/rejected batch
            # after the user changes the input limit or provider settings.
            # Ordinary session jobs remain idempotent and are never reopened.
            if event.migration_batch_id and record.get("status") in {
                "failed", "rejected", "quarantined", "skipped",
            }:
                history = list(record.get("phase_history", []))
                history.append({"phase": "queued", "at": self._now()})
                record = self._job_record(
                    event, job_id, attempts=0, status="pending", phase="queued",
                    phase_history=history,
                )
                self._write_job(record)
            else:
                return job_id
        if job_id in self._queued_ids or job_id in self._processing_ids:
            return job_id
        if record.get("quota_date") != self._today():
            if not self._reserve_daily_quota(record):
                # Keep a durable pending record for the next quota window.
                self._write_job(record, replace=exists)
                return job_id
            self._write_job(record, replace=exists)
        self._queue_job(job_id)
        return job_id

    async def resume_pending(self) -> int:
        if self._stopping or not self.config.enabled or not self.config.learning_enabled:
            return 0
        jobs_dir = self.store.root / "manifests" / "pending" / "jobs"
        if not jobs_dir.is_dir():
            return 0
        resumed = 0
        for path in sorted(jobs_dir.glob("learn-*.json")):
            job_id = path.stem
            try:
                record = self._read_job(job_id)
                if record.get("status") not in {"pending", "retry"}:
                    continue
                if job_id in self._queued_ids or job_id in self._processing_ids:
                    continue
                if record.get("quota_date") != self._today():
                    if not self._reserve_daily_quota(record):
                        break
                    self._write_job(record)
                if not self._queue_job(job_id):
                    break
                resumed += 1
            except FormatError:
                self._quarantine_job(job_id)
                continue
        return resumed

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if not self.config.enabled or not self.config.learning_enabled:
            raise DisabledError("Brain Memory learning is disabled")
        self._stopping = False
        await self.resume_pending()
        self._task = asyncio.create_task(self._run(), name="nanobot-brain-learning")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is None:
            return
        if self._task.cancelling() or self._task.cancelled():
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=self.config.shutdown_timeout_seconds)
        except asyncio.TimeoutError:
            self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def stats(self) -> dict:
        """Return bounded operational state for the dashboard and health checks."""
        return {
            "running": self._task is not None and not self._task.done(),
            "stopping": self._stopping,
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self.config.queue_capacity,
            "processing": len(self._processing_ids),
        }

    def cancel(self) -> None:
        """Revoke processing immediately; durable pending jobs remain for restart."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()

    async def _run(self) -> None:
        while not self._stopping or not self._queue.empty():
            try:
                job_id = await asyncio.wait_for(self._queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                await self.resume_pending()
                continue
            try:
                self._queued_ids.discard(job_id)
                self._processing_ids.add(job_id)
                await self.process(job_id)
            finally:
                self._processing_ids.discard(job_id)
                self._queue.task_done()
                await self.resume_pending()

    async def process(self, job_id: str) -> LearningResult:
        try:
            record = self._read_job(job_id)
        except FormatError:
            self._quarantine_job(job_id)
            return LearningResult(job_id, "failed", 0, error_type="FormatError")
        if record.get("status") not in {"pending", "retry"}:
            return LearningResult(job_id, record["status"], int(record.get("attempts", 0)))
        event = ConversationEvent.model_validate(record["event"])
        attempts = int(record.get("attempts", 0))
        created_at = str(record.get("created_at") or self._now())
        quota_date = record.get("quota_date")
        phase_history = list(record.get("phase_history", []))
        started_at = self._now()
        started_mono = time.monotonic()
        total_duration_ms = int(record.get("total_duration_ms", 0) or 0)
        last_duration_ms = 0

        def checkpoint(phase: str) -> None:
            phase_history.append({"phase": phase, "at": self._now()})
            self._write_job(self._job_record(
                event, job_id, attempts=attempts, phase=phase,
                created_at=created_at, quota_date=quota_date,
                phase_history=phase_history, started_at=started_at,
                last_duration_ms=last_duration_ms, total_duration_ms=total_duration_ms,
            ))

        try:
            if self._stopping or not self.config.enabled or not self.config.learning_enabled:
                raise DisabledError("Learning stopped")
            checkpoint("ai")
            generated = self.factory(event)
            manifest = await asyncio.wait_for(generated, self.config.learning_timeout_seconds) if inspect.isawaitable(generated) else generated
            if self._stopping or not self.config.enabled or not self.config.learning_enabled:
                raise DisabledError("Learning stopped")
            if not isinstance(manifest, Manifest):
                raise FormatError("Manifest factory returned an invalid result")
            checkpoint("validate")
            if self._stopping or not self.config.enabled or not self.config.learning_enabled:
                raise DisabledError("Learning stopped")
            checkpoint("promote")
            promotion = self.promoter.promote(manifest)
            checkpoint("completed")
            last_duration_ms = max(0, round((time.monotonic() - started_mono) * 1000))
            total_duration_ms += last_duration_ms
            finished_at = self._now()
            result = LearningResult(job_id, promotion.status, attempts + 1, promotion=promotion,
                                    duration_ms=last_duration_ms)
            if self.on_result:
                self.on_result(result)
            self._write_job(self._job_record(
                event, job_id, attempts=attempts + 1, status=promotion.status,
                created_at=created_at, quota_date=quota_date, phase="completed",
                fact_ids=promotion.fact_ids,
                created_fact_ids=promotion.created_fact_ids,
                promotion_sequence=promotion.promotion_sequence,
                phase_history=phase_history, started_at=started_at,
                last_finished_at=finished_at, last_duration_ms=last_duration_ms,
                total_duration_ms=total_duration_ms,
            ))
            return result
        except DisabledError:
            last_duration_ms = max(0, round((time.monotonic() - started_mono) * 1000))
            total_duration_ms += last_duration_ms
            checkpoint("skipped")
            self._write_job(self._job_record(
                event, job_id, attempts=attempts, status="skipped", phase="skipped",
                created_at=created_at, quota_date=quota_date, phase_history=phase_history,
                started_at=started_at, last_finished_at=self._now(),
                last_duration_ms=last_duration_ms, total_duration_ms=total_duration_ms,
            ))
            return LearningResult(job_id, "skipped", attempts, duration_ms=last_duration_ms)
        except Exception as exc:
            attempts += 1
            error_type = type(exc).__name__
            last_duration_ms = max(0, round((time.monotonic() - started_mono) * 1000))
            total_duration_ms += last_duration_ms
            if attempts < self.config.learning_max_attempts:
                checkpoint("retry")
                self._write_job(self._job_record(
                    event, job_id, attempts=attempts, error_type=error_type, phase="retry",
                    created_at=created_at, quota_date=quota_date, phase_history=phase_history,
                    started_at=started_at, last_finished_at=self._now(),
                    last_duration_ms=last_duration_ms, total_duration_ms=total_duration_ms,
                ))
                await asyncio.sleep(min(2 ** (attempts - 1), 8))
                self._queue_job(job_id)
                return LearningResult(job_id, "retry", attempts, error_type=error_type,
                                      duration_ms=last_duration_ms)
            self._write_job(self._job_record(
                event, job_id, attempts=attempts, status="failed", error_type=error_type, phase="failed",
                created_at=created_at, quota_date=quota_date, phase_history=phase_history + [{"phase": "failed", "at": self._now()}],
                started_at=started_at, last_finished_at=self._now(),
                last_duration_ms=last_duration_ms, total_duration_ms=total_duration_ms,
            ))
            logger.warning("Brain Memory learning job failed: {}", error_type)
            return LearningResult(job_id, "failed", attempts, error_type=error_type,
                                  duration_ms=last_duration_ms)
