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
from lorebase_store.telemetry import JOBS_CLAIMED

log = logging.getLogger(__name__)

GC_JOB = "gc"
"""Job type of a garbage collection (see `lorebase_store.gc.run_gc_job`)."""
SOURCE_SYNC_JOB = "source_sync"
"""Job type of a Source synchronization run by Scribe; payload `{"source": id, "dry_run"}`."""

EXTRACTION_JOB = "extraction"
"""Job type of a document extraction run by Scribe; payload from `extraction_payload`."""

INDEX_JOB = "store.index"
"""Job type of a per-Attribute index build or drop (see `lorebase_store.indexes`)."""
MATERIALIZE_JOB = "store.materialize"
"""Job type of an overlay turned into a full copy in batches; payload `{"branch": name}`
(see `lorebase_store.materialize.background`)."""
BRANCH_DELETE_JOB = "store.branch_delete"
"""Job type of the batched deletion of a hidden branch's rows; payload `{"branch": name}`
(see `lorebase_store.materialize.background`)."""
VAULT_ROTATE_JOB = "vault.rotate"
"""Job type of a vault key rotation pass over every Domain; payload `{"after": [domain, token]}`
or empty (see `lorebase_store.vault.rotation`)."""
SENSITIVITY_REFRESH_JOB = "store.sensitivity_refresh"
"""Job type of the batched rewrite of the titles and search vectors a branch materialized
before the default branch tagged an Attribute; payload `{"branch": name}`, plus `"resume":
[kind id, key]` for a continuation (see `lorebase_store.materialize.stale`)."""
EMBEDDINGS_JOB = "embeddings"
"""Job type of an embedding pass over a Domain's branches run by Scribe; payload from
`embeddings_payload`."""

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

_CANCEL_QUEUED = text(
    """
    UPDATE jobs SET status = 'cancelled', result = CAST(:result AS jsonb)
    WHERE status = 'queued' AND type = :type AND domain_id = :domain
      AND payload @> CAST(:match AS jsonb)
    """
)

_PENDING = text(
    """
    SELECT id FROM jobs
    WHERE type = :type AND domain_id IS NOT DISTINCT FROM CAST(:domain AS text)
      AND status IN ('queued', 'running') AND payload = CAST(:payload AS jsonb)
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

_TOUCH = text(
    "UPDATE jobs SET locked_at = :now "
    "WHERE id = ANY(CAST(:ids AS bigint[])) AND locked_by = :worker AND status = 'running'"
)

_RELEASE = text(
    "UPDATE jobs SET status = 'queued', run_after = :now, attempts = greatest(attempts - 1, 0), "
    "locked_by = NULL, locked_at = NULL WHERE id = :id AND status = 'running'"
)

_STALE = text(
    """
    UPDATE jobs SET status = 'queued', locked_by = NULL, locked_at = NULL
    WHERE status = 'running' AND locked_at < :before
    """
)

_DEPTH = text(
    """
    SELECT type,
           CASE WHEN status = 'queued' AND run_after > :now THEN 'retrying'
                ELSE CAST(status AS text) END,
           count(*)
    FROM jobs WHERE status IN ('queued', 'running')
    GROUP BY 1, 2
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


def source_sync_payload(source_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Payload of a `source_sync` job. Everything that queues one (Keeper's trigger, Scribe's
    scheduler) builds it here, so identical requests are recognized by `enqueue_job(unique)`."""
    return {"source": source_id, "dry_run": dry_run}


def extraction_payload(
    document_ids: Sequence[str], *, tiers: Sequence[int] | None = None, requested_by: str | None
) -> dict[str, Any]:
    """Payload of an `extraction` job: the documents, the tiers (`None`: Scribe's
    `scribe.extraction.tiers`) and who asked, recorded on the extraction job."""
    return {
        "documents": list(document_ids),
        "tiers": None if tiers is None else sorted(set(tiers)),
        "requested_by": requested_by,
    }


def embeddings_payload(
    *,
    branches: Sequence[str] | None = None,
    reindex: bool = False,
    resume: tuple[str, str, str] | None = None,
) -> dict[str, Any]:
    """Payload of an `embeddings` job: the branches (`None`: every materialized branch),
    whether every text is rendered again (`reindex`) and, for a continuation, the branch,
    Kind id and key to resume after. The scheduler queues the default, `{}`, so identical
    scheduled passes are recognized by `enqueue_job(unique)`."""
    payload: dict[str, Any] = {}
    if branches is not None:
        payload["branches"] = sorted(set(branches))
    if reindex:
        payload["reindex"] = True
    if resume is not None:
        payload["resume"] = list(resume)
    return payload


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
    same type, Domain (or no Domain) and payload is returned instead of adding a duplicate."""
    body = json.dumps(dict(payload or {}), sort_keys=True)
    async with rls_scope(session, bypass=True):
        if unique:
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


async def cancel_queued_jobs(
    session: AsyncSession,
    job_type: str,
    *,
    domain_id: str,
    match: Mapping[str, Any],
    reason: str,
) -> int:
    """Cancel the `queued` jobs of `job_type` in `domain_id` whose payload contains `match`;
    returns how many. Runs in the caller's transaction, in the Domain's row-level security
    scope. A job a worker has claimed is `running` and untouched: its handler decides."""
    result = await session.execute(
        _CANCEL_QUEUED,
        {
            "type": job_type,
            "domain": domain_id,
            "match": json.dumps(dict(match), sort_keys=True),
            "result": json.dumps({"detail": reason}),
        },
    )
    return int(getattr(result, "rowcount", 0) or 0)


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
        claimed = [JobRow(**row) for row in result.mappings()]
    for job in claimed:
        JOBS_CLAIMED.add(1, {"type": job.type})
    return claimed


async def queue_depth(
    session: AsyncSession, *, now: datetime | None = None
) -> dict[tuple[str, str], int]:
    """Jobs counted by (type, state) across Domains: `queued` (claimable now), `retrying`
    (queued with a `run_after` still ahead, such as a retry delay) or `running`."""
    async with rls_scope(session, bypass=True):
        rows = (await session.execute(_DEPTH, {"now": now or _now()})).all()
    return {(job_type, status): int(count) for job_type, status, count in rows}


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


async def touch_jobs(
    session: AsyncSession, worker_id: str, job_ids: Sequence[int], *, now: datetime | None = None
) -> int:
    """Heartbeat: refresh the lock time of running jobs held by `worker_id`, so that long jobs
    are not taken for lost by `requeue_stale`. Returns the number of jobs touched."""
    if not job_ids:
        return 0
    async with rls_scope(session, bypass=True):
        result = await session.execute(
            _TOUCH, {"ids": list(job_ids), "worker": worker_id, "now": now or _now()}
        )
        return int(getattr(result, "rowcount", 0) or 0)


async def release_job(session: AsyncSession, job: JobRow, *, now: datetime | None = None) -> None:
    """Put a claimed job back in the queue without counting the attempt (worker shutdown)."""
    async with rls_scope(session, bypass=True):
        await session.execute(_RELEASE, {"id": job.id, "now": now or _now()})


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
        "--types",
        nargs="+",
        default=[GC_JOB, INDEX_JOB, MATERIALIZE_JOB, BRANCH_DELETE_JOB, SENSITIVITY_REFRESH_JOB],
        help=f"Job types to run (default: {GC_JOB} {INDEX_JOB} {MATERIALIZE_JOB} "
        f"{BRANCH_DELETE_JOB} {SENSITIVITY_REFRESH_JOB}).",
    )
    return parser


async def _work(limit: int | None, types: Iterable[str]) -> int:
    from lorebase_platform.settings import load_settings
    from lorebase_store.db import create_engine
    from lorebase_store.gc import run_gc_job
    from lorebase_store.indexes import run_index_job
    from lorebase_store.materialize.background import run_branch_delete_job, run_materialize_job
    from lorebase_store.materialize.paths import refresh_stale_paths
    from lorebase_store.materialize.stale import run_sensitivity_refresh_job
    from lorebase_store.runtime import StoreRuntime
    from lorebase_store.settings import database_settings

    settings = load_settings()
    database = database_settings(settings)
    runtime = StoreRuntime.from_loaded(settings)
    engine: AsyncEngine = create_engine(database, pooled=False, statement_timeout_ms=0)
    # Index DDL needs a role that owns mat_records: the maintenance role when configured.
    maintenance: AsyncEngine | None = None
    if database.maintenance_dsn is not None:
        maintenance = create_engine(
            database,
            url=database.maintenance_dsn.get_secret_value(),
            pooled=False,
            statement_timeout_ms=0,
        )
    ddl = maintenance or engine
    known: dict[str, JobHandler] = {
        GC_JOB: lambda job: run_gc_job(engine, job, runtime=runtime),
        INDEX_JOB: lambda job: run_index_job(ddl, job),
        MATERIALIZE_JOB: lambda job: run_materialize_job(engine, job, runtime=runtime),
        BRANCH_DELETE_JOB: lambda job: run_branch_delete_job(engine, job, runtime=runtime),
        SENSITIVITY_REFRESH_JOB: lambda job: run_sensitivity_refresh_job(
            engine, job, runtime=runtime
        ),
    }
    unknown = [t for t in types if t not in known]
    if unknown:
        raise SystemExit(f"no handler for job type(s): {', '.join(unknown)}")
    try:
        if MATERIALIZE_JOB in types:
            await refresh_stale_paths(async_sessionmaker(engine), runtime=runtime)
        return await run_jobs(
            async_sessionmaker(engine, expire_on_commit=False),
            {t: known[t] for t in types},
            settings=runtime.settings.jobs,
            limit=limit,
        )
    finally:
        await engine.dispose()
        if maintenance is not None:
            await maintenance.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    ran = asyncio.run(_work(args.limit, args.types))
    sys.stdout.write(json.dumps({"jobs_run": ran}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
