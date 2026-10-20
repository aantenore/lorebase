"""Job queue helpers over the `jobs` table (design 7.4, store schema notes).

Workers claim `queued` rows with `FOR UPDATE SKIP LOCKED` (served by `ix_jobs_claimable`), run
them outside any transaction and record the outcome. A job that raises is requeued with a delay
until `jobs.max_attempts` is reached, then marked `failed`. A `running` job whose lock is older
than `jobs.stale_after_seconds` is assumed lost and requeued by `requeue_stale`.

Claiming spans Domains, so it runs with the row-level security bypass.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ARRAY, Text, bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_store.auth.scope import rls_scope
from lorebase_store.settings import JobSettings

log = logging.getLogger(__name__)

GC_JOB = "gc"
"""Job type of a garbage collection (see `lorebase_store.gc.run_gc_job`)."""

_COLUMNS = "id, type, domain_id, payload, priority, attempts, run_after, locked_by, created_at"

_CLAIM = text(
    f"""
    UPDATE jobs SET status = 'running', locked_by = :worker, locked_at = :now,
                    attempts = attempts + 1
    WHERE id IN (
        SELECT id FROM jobs
        WHERE status = 'queued' AND run_after <= :now
          AND (CAST(:types AS text[]) IS NULL OR type = ANY(CAST(:types AS text[])))
        ORDER BY priority DESC, run_after, id
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    RETURNING {_COLUMNS}
    """
).bindparams(bindparam("types", type_=ARRAY(Text())))

_ENQUEUE = text(
    """
    INSERT INTO jobs (type, domain_id, payload, priority, run_after)
    VALUES (:type, :domain, CAST(:payload AS jsonb), :priority, :run_after)
    RETURNING id
    """
)

_PENDING = text(
    """
    SELECT id FROM jobs
    WHERE type = :type AND domain_id = :domain AND status IN ('queued', 'running')
      AND payload = CAST(:payload AS jsonb)
    ORDER BY id LIMIT 1
    """
)

_COMPLETE = text(
    "UPDATE jobs SET status = 'succeeded', result = CAST(:result AS jsonb), "
    "locked_by = NULL, locked_at = NULL WHERE id = :id"
)

_RETRY = text(
    "UPDATE jobs SET status = 'queued', run_after = :run_after, "
    "result = CAST(:result AS jsonb), locked_by = NULL, locked_at = NULL WHERE id = :id"
)

_FAIL = text(
    "UPDATE jobs SET status = 'failed', result = CAST(:result AS jsonb), "
    "locked_by = NULL, locked_at = NULL WHERE id = :id"
)

_STALE = text(
    """
    UPDATE jobs SET status = 'queued', locked_by = NULL, locked_at = NULL
    WHERE status = 'running' AND locked_at < :before
    """
)


@dataclass(frozen=True, slots=True)
class JobRow:
    """A claimed job."""

    id: int
    type: str
    domain_id: str | None
    payload: dict[str, Any]
    priority: int
    attempts: int
    run_after: datetime
    locked_by: str | None
    created_at: datetime


JobHandler = Callable[[JobRow], Awaitable[Mapping[str, Any]]]
"""Runs a job and returns its JSON result; an exception fails the attempt."""


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _now() -> datetime:
    return datetime.now(UTC)


async def enqueue_job(
    session: AsyncSession,
    job_type: str,
    *,
    domain_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
    priority: int = 0,
    run_after: datetime | None = None,
    unique: bool = False,
) -> int:
    """Queue a job and return its id. With `unique`, an identical queued or running job of the
    same type, Domain and payload is returned instead of adding a duplicate."""
    body = json.dumps(dict(payload or {}), sort_keys=True)
    async with rls_scope(session, bypass=True):
        if unique and domain_id is not None:
            found = (
                await session.execute(
                    _PENDING, {"type": job_type, "domain": domain_id, "payload": body}
                )
            ).scalar_one_or_none()
            if found is not None:
                return int(found)
        result = await session.execute(
            _ENQUEUE,
            {
                "type": job_type,
                "domain": domain_id,
                "payload": body,
                "priority": priority,
                "run_after": run_after or _now(),
            },
        )
        return int(result.scalar_one())


async def claim_jobs(
    session: AsyncSession,
    worker_id: str,
    *,
    types: Sequence[str] | None = None,
    limit: int = 1,
    now: datetime | None = None,
) -> list[JobRow]:
    """Claim up to `limit` due jobs for `worker_id`; concurrent workers never get the same job."""
    async with rls_scope(session, bypass=True):
        result = await session.execute(
            _CLAIM,
            {
                "worker": worker_id,
                "now": now or _now(),
                "types": None if types is None else list(types),
                "limit": limit,
            },
        )
        return [JobRow(**row) for row in result.mappings()]


async def finish_job(
    session: AsyncSession,
    job: JobRow,
    *,
    result: Mapping[str, Any] | None = None,
    error: str | None = None,
    settings: JobSettings | None = None,
    now: datetime | None = None,
) -> str:
    """Record the outcome of a claimed job; returns the new status.

    With `error`, the job is requeued after `retry_delay_seconds` unless it has used its
    `max_attempts`, in which case it is marked failed.
    """
    settings = settings or JobSettings()
    async with rls_scope(session, bypass=True):
        if error is None:
            await session.execute(
                _COMPLETE, {"id": job.id, "result": json.dumps(dict(result or {}))}
            )
            return "succeeded"
        body = json.dumps({"error": error, "attempt": job.attempts})
        if job.attempts < settings.max_attempts:
            run_after = (now or _now()) + timedelta(seconds=settings.retry_delay_seconds)
            await session.execute(_RETRY, {"id": job.id, "run_after": run_after, "result": body})
            return "queued"
        await session.execute(_FAIL, {"id": job.id, "result": body})
        return "failed"


async def requeue_stale(
    session: AsyncSession, *, settings: JobSettings | None = None, now: datetime | None = None
) -> int:
    """Requeue running jobs whose lock is older than `stale_after_seconds`."""
    settings = settings or JobSettings()
    before = (now or _now()) - timedelta(seconds=settings.stale_after_seconds)
    async with rls_scope(session, bypass=True):
        result = await session.execute(_STALE, {"before": before})
        return int(getattr(result, "rowcount", 0) or 0)


async def run_jobs(
    sessions: async_sessionmaker[AsyncSession],
    handlers: Mapping[str, JobHandler],
    *,
    worker_id: str | None = None,
    settings: JobSettings | None = None,
    limit: int | None = None,
) -> int:
    """Claim and run due jobs of the handled types one by one until none is due (or `limit`
    jobs ran). Returns the number of jobs run."""
    worker = worker_id or default_worker_id()
    types = list(handlers)
    ran = 0
    while limit is None or ran < limit:
        async with sessions() as session, session.begin():
            claimed = await claim_jobs(session, worker, types=types, limit=1)
        if not claimed:
            return ran
        job = claimed[0]
        ran += 1
        try:
            result = await handlers[job.type](job)
            error = None
        except Exception as exc:
            log.exception("job %s (%s) failed", job.id, job.type)
            result, error = None, f"{type(exc).__name__}: {exc}"
        async with sessions() as session, session.begin():
            await finish_job(session, job, result=result, error=error, settings=settings)
    return ran


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lorebase_store.jobs", description="Run queued store jobs."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    work = commands.add_parser("work", help="Run due jobs until the queue is empty.")
    work.add_argument("--limit", type=int, default=None, help="Stop after this many jobs.")
    work.add_argument(
        "--types", nargs="+", default=[GC_JOB], help="Job types to run (default: gc)."
    )
    return parser


async def _work(limit: int | None, types: Iterable[str]) -> int:
    from lorebase_platform.settings import load_settings
    from lorebase_store.db import create_engine
    from lorebase_store.gc import run_gc_job
    from lorebase_store.runtime import StoreRuntime
    from lorebase_store.settings import database_settings, store_settings

    settings = load_settings()
    runtime = StoreRuntime.from_settings(store_settings(settings))
    engine: AsyncEngine = create_engine(
        database_settings(settings), pooled=False, statement_timeout_ms=0
    )
    known: dict[str, JobHandler] = {
        GC_JOB: lambda job: run_gc_job(engine, job, runtime=runtime),
    }
    unknown = [t for t in types if t not in known]
    if unknown:
        raise SystemExit(f"no handler for job type(s): {', '.join(unknown)}")
    try:
        return await run_jobs(
            async_sessionmaker(engine, expire_on_commit=False),
            {t: known[t] for t in types},
            settings=runtime.settings.jobs,
            limit=limit,
        )
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    ran = asyncio.run(_work(args.limit, args.types))
    sys.stdout.write(json.dumps({"jobs_run": ran}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
