"""The job loop (design 9, AC-18): claim jobs from the store queue and run them.

`concurrency` slots each claim one due job at a time (`FOR UPDATE SKIP LOCKED`, so replicas
never get the same job), run its handler outside any transaction and record the outcome with
`lorebase_store.jobs.finish_job`: a failure is retried after `store.jobs.retry_delay_seconds`
until `store.jobs.max_attempts`, except permanent failures (`PermanentJobError` and the types
passed as `permanent`), which fail at once. An outcome the store refuses to take (a dropped
connection, a statement timeout) is written once more after `outcome_retry_delay_s`; when that
fails too the job counts as `unrecorded`, the slot goes on claiming and the job is left to the
stale-lock reclaim below. Every `heartbeat_interval_s` the worker refreshes the lock time of
its running jobs; a worker that stops sending heartbeats (killed, frozen, cut off from the
database) loses its jobs to `requeue_stale` after `store.jobs.stale_after_seconds`, so jobs run
at least once and handlers must tolerate a second run.

A slot whose task ends with an error (nothing is expected to get past the guards above) is
logged and started again after `poll_interval_s`, so a process never runs with fewer slots than
configured for long; `lorebase.scribe.worker.slots` reports them. With `liveness_file` set the
loop rewrites that file every `liveness_interval_s` with its live slots, running jobs and the
time it last reached the store, for a container healthcheck or a probe
(`lorebase_scribe.liveness`).

On shutdown (SIGTERM), slots stop claiming and running jobs get `shutdown_grace_s` to finish.
The rest are cancelled; whatever has not stopped `shutdown_cancel_s` later is put back in the
queue anyway (`release_jobs`), so a stopping worker never leaves jobs `running`. Released jobs
do not count the attempt.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from opentelemetry.trace import Status, StatusCode
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_scribe.liveness import Liveness, remove_liveness, write_liveness
from lorebase_scribe.settings import WorkerSettings
from lorebase_scribe.telemetry import JOB_DURATION, JOBS, WORKER_SLOTS, tracer
from lorebase_store.jobs import (
    JobRow,
    claim_jobs,
    default_worker_id,
    finish_job,
    release_jobs,
    touch_jobs,
)
from lorebase_store.settings import JobSettings

log = logging.getLogger("lorebase.scribe.worker")

JobHandler = Callable[[JobRow], Awaitable[Mapping[str, Any]]]
"""Runs a job and returns its JSON result; an exception fails the attempt."""

UNRECORDED = "unrecorded"
"""Status of a run whose outcome the store would not take: the job still shows `running`
there until its lock goes stale, then runs again."""


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
        if settings.heartbeat_interval_s * 2 > jobs.stale_after_seconds:
            raise ValueError(
                f"scribe.worker.heartbeat_interval_s ({settings.heartbeat_interval_s:g}) must be "
                f"at most half of store.jobs.stale_after_seconds ({jobs.stale_after_seconds:g}), "
                "or running jobs are reclaimed from a live worker"
            )
        self._sessions = sessions
        self._handlers = dict(handlers)
        self._settings = settings
        self._jobs = jobs
        self._final = jobs.model_copy(update={"max_attempts": 1})
        self._permanent: tuple[type[Exception], ...] = (PermanentJobError, *permanent)
        self.worker_id = worker_id or default_worker_id()
        self._running: dict[int, JobRow] = {}
        self._slots: dict[int, asyncio.Task[None]] = {}
        self._restarts: dict[int, asyncio.TimerHandle] = {}
        self._store_reached_at: float | None = None

    @property
    def types(self) -> tuple[str, ...]:
        return tuple(self._handlers)

    @property
    def running(self) -> tuple[int, ...]:
        """Ids of the jobs running now."""
        return tuple(self._running)

    @property
    def live_slots(self) -> int:
        """Slots running the claim loop now (the others ended and wait to be started again)."""
        return sum(1 for task in self._slots.values() if not task.done())

    @property
    def store_reached_at(self) -> float | None:
        """Wall-clock time (`time.time()`) of the last claim, outcome or heartbeat round trip."""
        return self._store_reached_at

    async def claim(self) -> JobRow | None:
        async with self._sessions() as session, session.begin():
            claimed = await claim_jobs(session, self.worker_id, types=self.types, limit=1)
        self._store_reached_at = time.time()
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
        """Run a claimed job and record its outcome; returns the job's new status, or
        `UNRECORDED` when the store would not take it."""
        self._running[job.id] = job
        started = time.monotonic()
        attributes: dict[str, str | int] = {
            "lorebase.job.type": job.type,
            "lorebase.job.id": job.id,
        }
        if job.domain_id is not None:
            attributes["lorebase.domain"] = job.domain_id
        cancelled = False
        try:
            with tracer.start_as_current_span(
                f"scribe.job {job.type}", attributes=attributes
            ) as span:
                result: Mapping[str, Any] | None = None
                error: str | None = None
                settings = self._jobs
                try:
                    result = await self._handlers[job.type](job)
                except self._permanent as exc:
                    error, settings = f"{type(exc).__name__}: {exc}", self._final
                    span.record_exception(exc)
                except Exception as exc:
                    log.exception("job failed", extra={"job": job.id, "type": job.type})
                    error = f"{type(exc).__name__}: {exc}"
                    span.record_exception(exc)
                try:
                    status = await self._record(job, result=result, error=error, settings=settings)
                except Exception as exc:
                    status = UNRECORDED
                    span.record_exception(exc)
                    log.exception(
                        "job outcome not recorded: the store refused it twice; the job runs "
                        "again once its lock goes stale",
                        extra={"job": job.id, "type": job.type, "attempt": job.attempts},
                    )
                if error is not None:
                    span.set_status(Status(StatusCode.ERROR, error))
                elif status == UNRECORDED:
                    span.set_status(Status(StatusCode.ERROR, "outcome not recorded"))
                span.set_attribute("lorebase.job.status", status)
        except asyncio.CancelledError:
            # Shutdown: the job goes back to the queue. It stays tracked (and heartbeated)
            # until released, so `run` releases it if this release does not finish.
            cancelled = True
            await asyncio.shield(self.release(job))
            raise
        finally:
            if not cancelled:
                self._running.pop(job.id, None)
        duration = time.monotonic() - started
        JOBS.add(1, {"type": job.type, "status": status})
        JOB_DURATION.record(duration, {"type": job.type})
        log.log(
            logging.WARNING if error is not None or status == UNRECORDED else logging.INFO,
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

    async def _record(
        self,
        job: JobRow,
        *,
        result: Mapping[str, Any] | None,
        error: str | None,
        settings: JobSettings,
    ) -> str:
        """Write the outcome; a write the store refuses is tried once more after
        `outcome_retry_delay_s` (the pool reconnects meanwhile)."""
        try:
            return await self._finish(job, result=result, error=error, settings=settings)
        except Exception:
            log.warning(
                "recording the job outcome failed; trying once more",
                extra={"job": job.id, "type": job.type},
                exc_info=True,
            )
        await asyncio.sleep(self._settings.outcome_retry_delay_s)
        return await self._finish(job, result=result, error=error, settings=settings)

    async def _finish(
        self,
        job: JobRow,
        *,
        result: Mapping[str, Any] | None,
        error: str | None,
        settings: JobSettings,
    ) -> str:
        async with self._sessions() as session, session.begin():
            status = await finish_job(session, job, result=result, error=error, settings=settings)
        self._store_reached_at = time.time()
        return status

    async def release(self, *jobs: JobRow) -> int:
        """Put `jobs` (default: every running job) back in the queue without counting the
        attempt and stop tracking them; returns how many were still held by this worker."""
        chosen = list(jobs) or list(self._running.values())
        if not chosen:
            return 0
        try:
            async with self._sessions() as session, session.begin():
                released = await release_jobs(session, self.worker_id, [j.id for j in chosen])
        except Exception:
            log.exception(
                "job release failed; the jobs are reclaimed once their heartbeat goes stale",
                extra={"jobs": [j.id for j in chosen]},
            )
            return 0
        for job in chosen:
            self._running.pop(job.id, None)
            log.info("job released on shutdown", extra={"job": job.id, "type": job.type})
        return released

    async def heartbeat(self) -> int:
        """Refresh the lock time of the running jobs."""
        if not self._running:
            return 0
        ids = list(self._running)
        async with self._sessions() as session, session.begin():
            touched = await touch_jobs(session, self.worker_id, ids)
        self._store_reached_at = time.time()
        if touched < sum(1 for i in ids if i in self._running):  # not just finished meanwhile
            log.warning(
                "running jobs were reclaimed from this worker (heartbeat went stale); their "
                "outcome will not be recorded",
                extra={"worker": self.worker_id, "running": len(ids), "held": touched},
            )
        return touched

    def liveness(self) -> Liveness:
        """What the liveness file reports."""
        return Liveness(
            worker=self.worker_id,
            interval_s=self._settings.liveness_interval_s,
            live_slots=self.live_slots,
            configured_slots=self._settings.concurrency,
            running=self.running,
            store_reached_at=self._store_reached_at,
            written_at=time.time(),
        )

    async def _pause(self, stop: asyncio.Event, seconds: float) -> None:
        """Wait `seconds`, or less when `stop` is set meanwhile."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)

    async def _slot(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                job = await self.claim()
            except Exception:
                log.exception("claiming jobs failed")
                job = None
            if job is None:
                await self._pause(stop, self._settings.poll_interval_s)
                continue
            try:
                await self.execute(job)
            except Exception:
                # Handler and outcome failures are handled inside; the slot stays on and the
                # job, no longer heartbeated, is reclaimed once its lock goes stale.
                log.exception(
                    "job run failed; the slot continues", extra={"job": job.id, "type": job.type}
                )
                await self._pause(stop, self._settings.poll_interval_s)

    def _start_slot(self, index: int, stop: asyncio.Event) -> None:
        task = asyncio.create_task(self._slot(stop), name=f"scribe-slot-{index}")
        self._slots[index] = task
        task.add_done_callback(functools.partial(self._slot_ended, index, stop))
        self._report_slots()

    def _slot_ended(self, index: int, stop: asyncio.Event, task: asyncio.Task[None]) -> None:
        """A slot task finished: expected on shutdown; otherwise say why and start it again
        after `poll_interval_s` (at once, a slot that fails on entry would spin)."""
        self._report_slots()
        if stop.is_set() or task.cancelled():
            return
        log.error(
            "worker slot ended; starting it again",
            exc_info=task.exception(),
            extra={"worker": self.worker_id, "slot": index, "live": self.live_slots},
        )
        self._restarts[index] = asyncio.get_running_loop().call_later(
            self._settings.poll_interval_s, self._restart_slot, index, stop
        )

    def _restart_slot(self, index: int, stop: asyncio.Event) -> None:
        self._restarts.pop(index, None)
        if not stop.is_set():
            self._start_slot(index, stop)

    def _report_slots(self) -> None:
        WORKER_SLOTS.update(live=self.live_slots, configured=self._settings.concurrency)

    async def _heartbeats(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._pause(stop, self._settings.heartbeat_interval_s)
            try:
                await self.heartbeat()
            except Exception:
                log.exception("job heartbeat failed")

    async def _liveness(self, stop: asyncio.Event, path: Path) -> None:
        """Rewrite the liveness file every `liveness_interval_s`; remove it at the end, so a
        probe never trusts the file of a loop that stopped."""
        try:
            while not stop.is_set():
                try:
                    write_liveness(path, self.liveness())
                except OSError:
                    log.exception("liveness file not written", extra={"path": str(path)})
                await self._pause(stop, self._settings.liveness_interval_s)
        finally:
            with contextlib.suppress(OSError):
                remove_liveness(path)

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
        for index in range(self._settings.concurrency):
            self._start_slot(index, stop)
        background = [asyncio.create_task(self._heartbeats(stop), name="scribe-heartbeat")]
        if self._settings.liveness_file is not None:
            background.append(
                asyncio.create_task(
                    self._liveness(stop, Path(self._settings.liveness_file)),
                    name="scribe-liveness",
                )
            )
        await stop.wait()
        log.info(
            "worker stopping",
            extra={"worker": self.worker_id, "running": len(self._running)},
        )
        for handle in self._restarts.values():
            handle.cancel()
        self._restarts.clear()
        _, pending = await asyncio.wait(
            list(self._slots.values()), timeout=self._settings.shutdown_grace_s
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=self._settings.shutdown_cancel_s)
        # Jobs whose cancellation has not released them yet (a handler that does not stop).
        await self.release()
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        self._slots.clear()
        WORKER_SLOTS.clear()
        log.info("worker stopped", extra={"worker": self.worker_id})
