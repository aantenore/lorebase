"""Job types Scribe runs and their handlers.

| Type | Payload | Handler |
|------|---------|---------|
| `source_sync` | `{"source": id or api name, "dry_run": bool}`, Domain set | `SourceSync.run` |
| `webhook_delivery` | none | `lorebase_store.webhooks.deliver_pending`, one pass |
| `index_build` | none | `lorebase_store.indexes.run_index_jobs` (queued index builds) |
| `gc` | `{"dry_run": bool, "grace_seconds": int}`, Domain set | `lorebase_store.gc.run_gc_job` |
| `extraction` | reserved | not implemented yet: fails without retries |
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_scribe.settings import ScribeSettings
from lorebase_scribe.sources import PERMANENT_ERRORS, SourceSync
from lorebase_scribe.sources.connectors import ConnectorFactory, default_connector
from lorebase_scribe.sources.sync import utc_now
from lorebase_scribe.webhooks import HttpxSender
from lorebase_scribe.worker import JobHandler, PermanentJobError
from lorebase_store.gc import GcBusyError, GcError, run_gc_job
from lorebase_store.indexes import run_index_jobs
from lorebase_store.jobs import GC_JOB, SOURCE_SYNC_JOB, JobRow
from lorebase_store.runtime import StoreRuntime
from lorebase_store.webhooks import WebhookSettings, deliver_pending

WEBHOOK_DELIVERY_JOB = "webhook_delivery"
INDEX_BUILD_JOB = "index_build"
EXTRACTION_JOB = "extraction"

JOB_TYPES = (SOURCE_SYNC_JOB, WEBHOOK_DELIVERY_JOB, INDEX_BUILD_JOB, GC_JOB, EXTRACTION_JOB)
"""Every job type Scribe knows."""


@dataclass(frozen=True)
class JobContext:
    """What the handlers share: database, store runtime, settings and the HTTP client."""

    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    runtime: StoreRuntime
    settings: ScribeSettings
    webhooks: WebhookSettings
    http: httpx.AsyncClient
    worker_id: str
    connectors: ConnectorFactory = default_connector
    clock: Callable[[], datetime] = utc_now


def _domain(job: JobRow) -> str:
    if job.domain_id is None:
        raise PermanentJobError(f"a {job.type} job needs a Domain")
    return job.domain_id


def build_handlers(context: JobContext, types: Iterable[str] = ()) -> dict[str, JobHandler]:
    """Handlers of `types` (default: every type); unknown types raise `ValueError`."""

    async def source_sync(job: JobRow) -> Mapping[str, Any]:
        reference = job.payload.get("source")
        if not isinstance(reference, str) or not reference:
            raise PermanentJobError("a source_sync job needs a `source`")
        sync = SourceSync(
            context.sessions,
            context.runtime,
            context.settings.sources,
            principal=context.settings.worker.principal,
            connectors=context.connectors,
            clock=context.clock,
        )
        report = await sync.run(
            _domain(job), reference, dry_run=bool(job.payload.get("dry_run", False))
        )
        return report.as_dict()

    async def webhook_delivery(job: JobRow) -> Mapping[str, Any]:
        report = await deliver_pending(
            context.sessions,
            context.webhooks,
            HttpxSender(context.http),
            writes=context.runtime.settings.writes,
        )
        return dataclasses.asdict(report)

    async def index_build(job: JobRow) -> Mapping[str, Any]:
        outcomes = await run_index_jobs(
            context.engine, context.runtime.settings.indexes, worker=context.worker_id
        )
        return {
            "ran": len(outcomes),
            "outcomes": [
                {
                    "job": o.job_id,
                    "action": o.action.value,
                    "name": o.name,
                    "status": o.status.value,
                    "detail": o.detail,
                }
                for o in outcomes
            ],
        }

    async def gc(job: JobRow) -> Mapping[str, Any]:
        _domain(job)
        try:
            return await run_gc_job(context.engine, job, runtime=context.runtime)
        except GcBusyError:
            raise
        except GcError as error:
            raise PermanentJobError(str(error)) from None

    async def extraction(job: JobRow) -> Mapping[str, Any]:
        raise PermanentJobError("extraction jobs are not implemented yet")

    available: dict[str, JobHandler] = {
        SOURCE_SYNC_JOB: source_sync,
        WEBHOOK_DELIVERY_JOB: webhook_delivery,
        INDEX_BUILD_JOB: index_build,
        GC_JOB: gc,
        EXTRACTION_JOB: extraction,
    }
    chosen = list(types) or list(available)
    unknown = sorted(set(chosen) - available.keys())
    if unknown:
        raise ValueError(f"unknown job type(s): {', '.join(unknown)}")
    return {name: available[name] for name in chosen}


PERMANENT_JOB_ERRORS: tuple[type[Exception], ...] = PERMANENT_ERRORS
"""Handler failures the worker fails at once instead of retrying."""
