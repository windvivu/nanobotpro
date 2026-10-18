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


# Knowledge the admin publishes for customer service is a deliberate action: a slow
# model gets the longest wait the config allows, not the background default.
KB_LEARNING_TIMEOUT_S = 300


def learning_timeout(config, *, published_kb: bool) -> int:
    if published_kb:
        return max(config.learning_timeout_seconds, KB_LEARNING_TIMEOUT_S)
    return config.learning_timeout_seconds


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
        public_customer_mode: bool = False,
    ):
        self.store = store
        self.promoter = promoter
        self.factory = manifest_factory
        self.config = config or store.config
        self.on_result = on_result
        self.public_customer_mode = public_customer_mode
        self._queue: asyncio.PriorityQueue[tuple[int, int, str]] = asyncio.PriorityQueue(maxsize=self.config.queue_capacity)
        self._queue_sequence = 0
        self._queued_ids: set[str] = set()
        self._processing_ids: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        # Compact metadata only: never cache paths or job payloads. All actual
        # reads/writes still go through BrainStore's path security checks.
        self._job_index: dict[str, tuple[str, str, bool]] = {}
        self._index_ready = False
        self._quota_day = ""
        self._quota_count = 0
        self._scan_lock = asyncio.Lock()

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
        self._remember_job(record)

    def _remember_job(self, record: dict) -> None:
        job_id = record["job_id"]
        previous = self._job_index.get(job_id)
        current = (record["status"], record.get("quota_date", ""), self._priority_kb(record))
        if self._quota_day:
            self._quota_count += int(current[1] == self._quota_day and not current[2])
            if previous:
                self._quota_count -= int(previous[1] == self._quota_day and not previous[2])
        self._job_index[job_id] = current

    def _forget_job(self, job_id: str) -> None:
        previous = self._job_index.pop(job_id, None)
        if previous and self._quota_day:
            self._quota_count -= int(previous[1] == self._quota_day and not previous[2])

    def _job_names(self) -> list[str]:
        return sorted(path.stem for path in
                      (self.store.root / "manifests/pending/jobs").glob("learn-*.json"))

    async def _scan_jobs(self) -> list[dict]:
        """Discover new files and refresh unfinished records, yielding in batches.

        Index updates run on the event loop, like enqueue/checkpoints, so a
        background snapshot cannot overwrite a newer checkpoint.
        """
        records = []
        names = self._job_names()
        for job_id in self._job_index.keys() - set(names):
            self._forget_job(job_id)
        reads = 0
        today = self._today()
        # Asked only once the index is complete; before that the count would build it synchronously.
        exhausted = (self._index_ready
                     and self._daily_reserved_jobs() >= self.config.learning_daily_job_limit)
        for job_id in names:
            previous = self._job_index.get(job_id)
            if previous:
                if previous[0] not in {"pending", "retry"}:
                    continue
                # An unfinished job needs no read while it is already on its way, or while
                # it is an ordinary job that cannot get a slot today (it may wait for hours).
                if job_id in self._queued_ids or job_id in self._processing_ids:
                    continue
                if exhausted and not previous[2] and previous[1] != today:
                    continue
            try:
                record = self._read_job(job_id)
                self._remember_job(record)
                if record["status"] in {"pending", "retry"}:
                    records.append(record)
            except FormatError:
                self._quarantine_job(job_id)
            reads += 1
            if reads % 8 == 0:
                await asyncio.sleep(0)
        self._index_ready = True
        return records

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
                self._forget_job(job_id)
        except (OSError, ValueError):
            logger.warning("Brain job quarantine unavailable: {}", job_id)

    def _priority_kb(self, record: dict) -> bool:
        """Derive priority from persisted provenance, never from a caller flag."""
        from nanobot.brain_memory.config import is_public_kb_source

        if not self.public_customer_mode:
            return False
        try:
            event = ConversationEvent.model_validate(record["event"])
        except (KeyError, TypeError, ValueError):
            return False
        return bool(event.migration_batch_id and is_public_kb_source(event.source.source_ref))

    def _daily_reserved_jobs(self) -> int:
        """Count once per day from metadata; checkpoints maintain the total."""
        if not self._index_ready:
            # Synchronous callers (diagnostics/tests). Async entry points build
            # the index cooperatively before requesting a reservation.
            for job_id in self._job_names():
                try:
                    self._remember_job(self._read_job(job_id))
                except FormatError:
                    self._quarantine_job(job_id)
            self._index_ready = True
        today = self._today()
        if self._quota_day != today:
            self._quota_count = sum(day == today and not priority
                                    for _, day, priority in self._job_index.values())
            self._quota_day = today
        return self._quota_count

    def _reserve_daily_quota(self, record: dict) -> bool:
        """Reserve one logical job for today, once, before queueing it."""
        if self._priority_kb(record):
            return True
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
            priority = 0 if self._priority_kb(self._read_job(job_id)) else 1
            self._queue.put_nowait((priority, self._queue_sequence, job_id))
            self._queue_sequence += 1
        except asyncio.QueueFull:
            return False
        self._queued_ids.add(job_id)
        return True

    async def enqueue(self, event: ConversationEvent) -> str:
        if self._stopping or not self.config.enabled or not self.config.learning_enabled:
            raise DisabledError("Brain Memory learning is disabled")
        if not event.selected:
            raise ValueError("Only explicitly selected conversation events may be queued")
        if not self._index_ready:
            async with self._scan_lock:
                await self._scan_jobs()
            if self._stopping or not self.config.enabled or not self.config.learning_enabled:
                raise DisabledError("Brain Memory learning is disabled")
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
        async with self._scan_lock:
            return await self._resume_pending()

    async def _resume_pending(self) -> int:
        if self._stopping or not self.config.enabled or not self.config.learning_enabled:
            return 0
        resumed = 0
        records = await self._scan_jobs()
        if self._stopping or not self.config.enabled or not self.config.learning_enabled:
            return 0
        records.sort(key=lambda record: not self._priority_kb(record))
        quota_exhausted = False
        for record in records:
            job_id = record["job_id"]
            try:
                # A checkpoint/enqueue may run at a cooperative scan yield.
                if self._job_index.get(job_id, (None,))[0] not in {"pending", "retry"}:
                    continue
                if record.get("status") not in {"pending", "retry"}:
                    continue
                if job_id in self._queued_ids or job_id in self._processing_ids:
                    continue
                if record.get("quota_date") != self._today():
                    # One failed reservation settles the day for every other ordinary
                    # job in this pass: asking again would recount the whole job
                    # directory once per waiting job.
                    if quota_exhausted and not self._priority_kb(record):
                        continue
                    if not self._reserve_daily_quota(record):
                        quota_exhausted = True
                        continue
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
        if self._stopping or (self._task is not None and not self._task.done()):
            return
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
                _, _, job_id = await asyncio.wait_for(self._queue.get(), timeout=0.2)
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
            timeout = learning_timeout(self.config, published_kb=self._priority_kb(record))
            manifest = await asyncio.wait_for(generated, timeout) if inspect.isawaitable(generated) else generated
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
