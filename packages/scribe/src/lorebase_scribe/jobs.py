"""Job types Scribe runs and their handlers.

| Type | Payload | Handler |
|------|---------|---------|
| `source_sync` | `{"source": id or api name, "dry_run": bool}`, Domain set | `SourceSync.run` |
| `webhook_delivery` | none | `lorebase_store.webhooks.deliver_pending`, one pass |
| `store.index` | `{"action", "name", "branch", "kind"}`, Domain set | `indexes.run_index_job` |
| `gc` | `{"dry_run": bool, "grace_seconds": int}`, Domain set | `lorebase_store.gc.run_gc_job` |
| `store.materialize` | `{"branch"}`, Domain set | `materialize.run_materialize_job` |
| `extraction` | `{"documents", "tiers", "requested_by"}`, Domain set | `ExtractionRun.run` |

An `extraction` job reads `documents` (ids) with `tiers` (a list, or null for
`scribe.extraction.tiers`); `requested_by` is recorded on the extraction job.

Index DDL runs on `JobContext.ddl_engine` (the maintenance role) when one is configured.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_inference import InferenceSettings
from lorebase_scribe.extraction.pipeline import PERMANENT_ERRORS as EXTRACTION_ERRORS
from lorebase_scribe.extraction.pipeline import ExtractionModels, ExtractionRun
from lorebase_scribe.settings import ScribeSettings
from lorebase_scribe.sources import PERMANENT_ERRORS, SourceSync
from lorebase_scribe.sources.connectors import ConnectorFactory, default_connector
from lorebase_scribe.sources.sync import utc_now
from lorebase_scribe.webhooks import HttpxSender
from lorebase_scribe.worker import JobHandler, PermanentJobError
from lorebase_store.gc import GcBusyError, GcError, run_gc_job
from lorebase_store.indexes import run_index_job
from lorebase_store.jobs import (
    EXTRACTION_JOB,
    GC_JOB,
    INDEX_JOB,
    MATERIALIZE_JOB,
    SOURCE_SYNC_JOB,
    JobRow,
)
from lorebase_store.materialize import run_materialize_job
from lorebase_store.runtime import StoreRuntime
from lorebase_store.webhooks import WebhookSettings, deliver_pending

WEBHOOK_DELIVERY_JOB = "webhook_delivery"

JOB_TYPES = (
    SOURCE_SYNC_JOB,
    WEBHOOK_DELIVERY_JOB,
    INDEX_JOB,
    MATERIALIZE_JOB,
    GC_JOB,
    EXTRACTION_JOB,
)
"""Every job type Scribe knows."""


@dataclass(frozen=True)
class JobContext:
    """What the handlers share: database, store runtime, settings and the HTTP client.

    `ddl_engine` runs per-Attribute index DDL: the maintenance engine
    (`database.maintenance_dsn`) when configured, else `engine`. `extraction_models` replaces
    the models built from `inference` (tests)."""

    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    runtime: StoreRuntime
    settings: ScribeSettings
    webhooks: WebhookSettings
    http: httpx.AsyncClient
    worker_id: str
    connectors: ConnectorFactory = default_connector
    clock: Callable[[], datetime] = utc_now
    ddl_engine: AsyncEngine | None = None
    inference: InferenceSettings = field(default_factory=InferenceSettings)
    extraction_models: Callable[[Sequence[int]], ExtractionModels] | None = None


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

    async def index(job: JobRow) -> Mapping[str, Any]:
        _domain(job)
        return await run_index_job(context.ddl_engine or context.engine, job)

    async def materialize(job: JobRow) -> Mapping[str, Any]:
        _domain(job)
        return await run_materialize_job(context.engine, job, runtime=context.runtime)

    async def gc(job: JobRow) -> Mapping[str, Any]:
        _domain(job)
        try:
            return await run_gc_job(context.engine, job, runtime=context.runtime)
        except GcBusyError:
            raise
        except GcError as error:
            raise PermanentJobError(str(error)) from None

    async def extraction(job: JobRow) -> Mapping[str, Any]:
        documents = job.payload.get("documents")
        if not isinstance(documents, list) or not documents:
            raise PermanentJobError("an extraction job needs `documents`")
        tiers = job.payload.get("tiers")
        if tiers is not None and not isinstance(tiers, list):
            raise PermanentJobError("`tiers` must be a list")
        run = ExtractionRun(
            context.sessions,
            context.runtime,
            context.settings.extraction,
            context.inference,
            principal=context.settings.worker.principal,
            models=context.extraction_models,
            clock=context.clock,
        )
        requested_by = job.payload.get("requested_by")
        report = await run.run(
            _domain(job),
            [str(d) for d in documents],
            tiers=None if tiers is None else [int(t) for t in tiers],
            requested_by=str(requested_by) if requested_by else None,
        )
        return report.as_dict()

    available: dict[str, JobHandler] = {
        SOURCE_SYNC_JOB: source_sync,
        WEBHOOK_DELIVERY_JOB: webhook_delivery,
        INDEX_JOB: index,
        MATERIALIZE_JOB: materialize,
        GC_JOB: gc,
        EXTRACTION_JOB: extraction,
    }
    chosen = list(types) or list(available)
    unknown = sorted(set(chosen) - available.keys())
    if unknown:
        raise ValueError(f"unknown job type(s): {', '.join(unknown)}")
    return {name: available[name] for name in chosen}


PERMANENT_JOB_ERRORS: tuple[type[Exception], ...] = (*PERMANENT_ERRORS, *EXTRACTION_ERRORS)
"""Handler failures the worker fails at once instead of retrying."""
