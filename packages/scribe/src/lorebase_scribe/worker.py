"""The job loop (design 9, AC-18): claim jobs from the store queue and run them.

`concurrency` slots each claim one due job at a time (`FOR UPDATE SKIP LOCKED`, so replicas
never get the same job), run its handler outside any transaction and record the outcome with
`lorebase_store.jobs.finish_job`: a failure is retried after `store.jobs.retry_delay_seconds`
until `store.jobs.max_attempts`, except permanent failures (`PermanentJobError` and the types
passed as `permanent`), which fail at once. A heartbeat refreshes the lock time of running
jobs so `requeue_stale` never takes a long job for lost. On shutdown, slots stop claiming,
running jobs get `shutdown_grace_s` to finish, and the rest are put back in the queue without
counting the attempt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from opentelemetry.trace import Status, StatusCode
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_scribe.settings import WorkerSettings
from lorebase_scribe.telemetry import JOB_DURATION, JOBS, tracer
from lorebase_store.jobs import (
    JobRow,
    claim_jobs,
    default_worker_id,
    finish_job,
    release_job,
    touch_jobs,
)
from lorebase_store.settings import JobSettings

log = logging.getLogger("lorebase.scribe.worker")

JobHandler = Callable[[JobRow], Awaitable[Mapping[str, Any]]]
"""Runs a job and returns its JSON result; an exception fails the attempt."""


class PermanentJobError(Exception):
    """The job can never succeed as queued: it is failed without retries."""


class Worker:
    """Claims and runs jobs of the handled types."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        handlers: Mapping[str, JobHandler],
        *,
        settings: WorkerSettings,
        jobs: JobSettings,
        worker_id: str | None = None,
        permanent: tuple[type[Exception], ...] = (),
    ) -> None:
        if not handlers:
            raise ValueError("a worker needs at least one job type")
        self._sessions = sessions
        self._handlers = dict(handlers)
        self._settings = settings
        self._jobs = jobs
        self._final = jobs.model_copy(update={"max_attempts": 1})
        self._permanent: tuple[type[Exception], ...] = (PermanentJobError, *permanent)
        self.worker_id = worker_id or default_worker_id()
        self._running: dict[int, JobRow] = {}

    @property
    def types(self) -> tuple[str, ...]:
        return tuple(self._handlers)

    @property
    def running(self) -> tuple[int, ...]:
        """Ids of the jobs running now."""
        return tuple(self._running)

    async def claim(self) -> JobRow | None:
        async with self._sessions() as session, session.begin():
            claimed = await claim_jobs(session, self.worker_id, types=self.types, limit=1)
        return claimed[0] if claimed else None

    async def run_once(self, *, limit: int | None = None) -> int:
        """Run due jobs one by one until none is due (or `limit` ran); returns how many ran."""
        ran = 0
        while limit is None or ran < limit:
            job = await self.claim()
            if job is None:
                break
            await self.execute(job)
            ran += 1
        return ran

    async def execute(self, job: JobRow) -> str:
        """Run a claimed job and record its outcome; returns the job's new status."""
        self._running[job.id] = job
        started = time.monotonic()
        attributes: dict[str, str | int] = {
            "lorebase.job.type": job.type,
            "lorebase.job.id": job.id,
        }
        if job.domain_id is not None:
            attributes["lorebase.domain"] = job.domain_id
        try:
            with tracer.start_as_current_span(
                f"scribe.job {job.type}", attributes=attributes
            ) as span:
                result: Mapping[str, Any] | None = None
                error: str | None = None
                settings = self._jobs
                try:
                    result = await self._handlers[job.type](job)
                except asyncio.CancelledError:
                    await asyncio.shield(self._release(job))
                    raise
                except self._permanent as exc:
                    error, settings = f"{type(exc).__name__}: {exc}", self._final
                    span.record_exception(exc)
                except Exception as exc:
                    log.exception("job failed", extra={"job": job.id, "type": job.type})
                    error = f"{type(exc).__name__}: {exc}"
                    span.record_exception(exc)
                async with self._sessions() as session, session.begin():
                    status = await finish_job(
                        session, job, result=result, error=error, settings=settings
                    )
                if error is not None:
                    span.set_status(Status(StatusCode.ERROR, error))
                span.set_attribute("lorebase.job.status", status)
        finally:
            self._running.pop(job.id, None)
        duration = time.monotonic() - started
        JOBS.add(1, {"type": job.type, "status": status})
        JOB_DURATION.record(duration, {"type": job.type})
        log.log(
            logging.WARNING if error is not None else logging.INFO,
            "job finished",
            extra={
                "job": job.id,
                "type": job.type,
                "domain": job.domain_id,
                "attempt": job.attempts,
                "status": status,
                "error": error,
                "duration_ms": int(duration * 1000),
            },
        )
        return status

    async def _release(self, job: JobRow) -> None:
        try:
            async with self._sessions() as session, session.begin():
                await release_job(session, job)
            log.info("job released on shutdown", extra={"job": job.id, "type": job.type})
        except Exception:
            log.exception("job release failed; it is requeued when its lock goes stale")

    async def heartbeat(self) -> int:
        """Refresh the lock time of the running jobs."""
        if not self._running:
            return 0
        async with self._sessions() as session, session.begin():
            return await touch_jobs(session, self.worker_id, list(self._running))

    async def _slot(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                job = await self.claim()
            except Exception:
                log.exception("claiming jobs failed")
                job = None
            if job is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._settings.poll_interval_s)
                continue
            await self.execute(job)

    async def _heartbeats(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._settings.heartbeat_interval_s)
            try:
                await self.heartbeat()
            except Exception:
                log.exception("job heartbeat failed")

    async def run(self, stop: asyncio.Event) -> None:
        """Run until `stop` is set, then shut down gracefully."""
        log.info(
            "worker started",
            extra={
                "worker": self.worker_id,
                "types": list(self.types),
                "concurrency": self._settings.concurrency,
            },
        )
        slots = [
            asyncio.create_task(self._slot(stop), name=f"scribe-slot-{i}")
            for i in range(self._settings.concurrency)
        ]
        beat = asyncio.create_task(self._heartbeats(stop), name="scribe-heartbeat")
        await stop.wait()
        _, pending = await asyncio.wait(slots, timeout=self._settings.shutdown_grace_s)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)
        log.info("worker stopped", extra={"worker": self.worker_id})
