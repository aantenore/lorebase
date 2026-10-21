"""Periodic jobs with leader election (design 9).

Every Scribe process may run a scheduler; the one holding a session-level Postgres advisory
lock (key derived from `scribe.scheduler.lock_name`, held on a dedicated connection) is the
leader, so replicas never queue the same periodic job twice. A leader whose connection dies
loses the lock with it and another process takes over at its next tick.

Each tick of the leader:

- requeues jobs whose lock went stale (`store.jobs.stale_after_seconds`);
- queues a `webhook_delivery` pass every `webhook_interval_s` (an identical queued job is
  reused, so a backlog never piles up);
- queues a `gc` job per Domain at each firing of `gc_schedule`;
- queues a `source_sync` job for each Source whose `schedule` fired, read from the schema of
  the Domain's default branch.

A cron job is due when its latest firing time is later than the `run_after` of the newest job
it queued before (jobs are queued with `run_after` set to the firing time) or, with no such
job, later than the moment this process became leader. Missed firings are not replayed: one
job covers them.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import ElementStatus
from lorebase_scribe.cron import CronError, parse_cron
from lorebase_scribe.jobs import WEBHOOK_DELIVERY_JOB
from lorebase_scribe.settings import SchedulerSettings
from lorebase_scribe.telemetry import SCHEDULED, tracer
from lorebase_store.auth.scope import rls_scope
from lorebase_store.commit.schema import load_compiled
from lorebase_store.jobs import (
    GC_JOB,
    SOURCE_SYNC_JOB,
    enqueue_job,
    requeue_stale,
    source_sync_payload,
)
from lorebase_store.repositories import DomainRepository, MaterializedRepository, scope_to_domain
from lorebase_store.settings import JobSettings, WriteSettings

log = logging.getLogger("lorebase.scribe.scheduler")

_LAST_FIRE = text(
    """
    SELECT max(run_after) FROM jobs
    WHERE type = :type AND domain_id IS NOT DISTINCT FROM CAST(:domain AS text)
      AND (CAST(:source AS text) IS NULL OR payload ->> 'source' = CAST(:source AS text))
      AND coalesce(payload ->> 'dry_run', 'false') = :dry_run
    """
)


def lock_key(name: str) -> int:
    """Signed 64-bit advisory lock key of a name."""
    return int.from_bytes(hashlib.blake2b(name.encode(), digest_size=8).digest(), signed=True)


def utc_now() -> datetime:
    return datetime.now(UTC)


class Scheduler:
    """Leader election and periodic job queueing."""

    def __init__(
        self,
        engine: AsyncEngine,
        sessions: async_sessionmaker[AsyncSession],
        *,
        settings: SchedulerSettings,
        jobs: JobSettings,
        writes: WriteSettings,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._engine = engine
        self._sessions = sessions
        self._settings = settings
        self._jobs = jobs
        self._writes = writes
        self._clock = clock
        self._key = lock_key(settings.lock_name)
        self._connection: AsyncConnection | None = None
        self._leader_since: datetime | None = None
        self._last: dict[str, datetime] = {}

    @property
    def is_leader(self) -> bool:
        return self._connection is not None

    async def acquire(self) -> bool:
        """Become (or stay) the leader; false when another process holds the lock."""
        if self._connection is not None:
            try:
                await self._connection.execute(text("SELECT 1"))
                return True
            except Exception:
                log.warning("scheduler lock connection lost")
                await self._drop()
        connection = await self._engine.connect()
        try:
            auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
            acquired = (
                await auto.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": self._key})
            ).scalar_one()
        except Exception:
            await connection.close()
            raise
        if not acquired:
            await connection.close()
            return False
        self._connection = auto
        self._leader_since = self._clock()
        self._last.clear()
        log.info("scheduler leadership acquired", extra={"lock": self._settings.lock_name})
        return True

    async def _drop(self) -> None:
        connection, self._connection = self._connection, None
        self._leader_since = None
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()

    async def release(self) -> None:
        """Give up leadership."""
        if self._connection is not None:
            with contextlib.suppress(Exception):
                await self._connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": self._key}
                )
            log.info("scheduler leadership released")
        await self._drop()

    async def tick(self) -> list[int]:
        """One pass: queue what is due when leader. Returns the ids of the queued jobs."""
        if not self._settings.enabled or not await self.acquire():
            return []
        now = self._clock()
        queued: list[int] = []
        with tracer.start_as_current_span("scribe.scheduler.tick"):
            async with self._sessions() as session, session.begin():
                stale = await requeue_stale(session, settings=self._jobs, now=now)
                if stale:
                    log.warning("stale jobs requeued", extra={"jobs": stale})
                queued.extend(await self._intervals(session, now))
                queued.extend(await self._domains(session, now))
        if queued:
            SCHEDULED.add(len(queued))
        return queued

    async def _intervals(self, session: AsyncSession, now: datetime) -> list[int]:
        queued = []
        for job_type, interval in ((WEBHOOK_DELIVERY_JOB, self._settings.webhook_interval_s),):
            last = self._last.get(job_type)
            if interval <= 0 or (last is not None and (now - last).total_seconds() < interval):
                continue
            queued.append(await enqueue_job(session, job_type, unique=True))
            self._last[job_type] = now
        return queued

    async def _due(
        self,
        session: AsyncSession,
        job_type: str,
        domain: str,
        fire: datetime,
        *,
        source: str | None = None,
        dry_run: bool = False,
    ) -> bool:
        """Whether a firing is later than the newest job of the same kind (a dry run triggered
        by hand does not stand in for a real one)."""
        parameters = {
            "type": job_type,
            "domain": domain,
            "source": source,
            "dry_run": "true" if dry_run else "false",
        }
        async with rls_scope(session, bypass=True):
            last = (await session.execute(_LAST_FIRE, parameters)).scalar_one_or_none()
        anchor = last if last is not None else self._leader_since
        return anchor is None or fire > anchor

    async def _domains(self, session: AsyncSession, now: datetime) -> list[int]:
        gc = parse_cron(self._settings.gc_schedule) if self._settings.gc_schedule else None
        queued: list[int] = []
        after: str | None = None
        while True:
            domains = await DomainRepository(session).list(
                limit=self._settings.domain_batch, after=after
            )
            if not domains:
                return queued
            for domain in domains:
                if gc is not None:
                    fire = gc.previous(now)
                    dry_run = self._settings.gc_dry_run
                    if await self._due(session, GC_JOB, domain.id, fire, dry_run=dry_run):
                        queued.append(
                            await enqueue_job(
                                session,
                                GC_JOB,
                                domain_id=domain.id,
                                payload={"dry_run": dry_run},
                                run_after=fire,
                                unique=True,
                            )
                        )
                if self._settings.source_schedules:
                    queued.extend(
                        await self._sources(session, domain.id, domain.default_branch, now)
                    )
            after = domains[-1].id

    async def _sources(
        self, session: AsyncSession, domain_id: str, branch: str, now: datetime
    ) -> list[int]:
        await scope_to_domain(session, domain_id)
        compiled = await MaterializedRepository(
            session, domain_id, branch, writes=self._writes
        ).schema()
        if compiled is None:
            return []
        queued = []
        for source in load_compiled(compiled.compiled).sources.values():
            if source.schedule is None or source.status is ElementStatus.DEPRECATED:
                continue
            try:
                fire = parse_cron(source.schedule).previous(now)
            except CronError as error:
                log.warning(
                    "invalid Source schedule",
                    extra={"domain": domain_id, "source": source.api_name, "error": str(error)},
                )
                continue
            if await self._due(session, SOURCE_SYNC_JOB, domain_id, fire, source=source.id):
                queued.append(
                    await enqueue_job(
                        session,
                        SOURCE_SYNC_JOB,
                        domain_id=domain_id,
                        payload=source_sync_payload(source.id),
                        run_after=fire,
                        unique=True,
                    )
                )
        return queued

    async def run(self, stop: asyncio.Event) -> None:
        """Tick every `tick_s` until `stop` is set, then release the lock."""
        try:
            while not stop.is_set():
                try:
                    await self.tick()
                except Exception:
                    log.exception("scheduler tick failed")
                    await self._drop()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._settings.tick_s)
        finally:
            await self.release()
