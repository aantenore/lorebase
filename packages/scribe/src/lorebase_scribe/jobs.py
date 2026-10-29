"""Job types Scribe runs and their handlers.

| Type | Payload | Handler |
|------|---------|---------|
| `source_sync` | `{"source": id or api name, "dry_run": bool}`, Domain set | `SourceSync.run` |
| `webhook_delivery` | none | `lorebase_store.webhooks.deliver_pending`, one pass |
| `store.index` | `{"action", "name", "branch", "kind"}`, Domain set | `indexes.run_index_job` |
| `gc` | `{"dry_run": bool, "grace_seconds": int}`, Domain set | `lorebase_store.gc.run_gc_job` |
| `store.materialize` | `{"branch"}`, Domain set | `materialize.run_materialize_job` |
| `store.branch_delete` | `{"branch"}`, Domain set | `materialize.run_branch_delete_job` |
| `store.sensitivity_refresh` | `{"branch", "resume"}`, Domain set | `run_sensitivity_refresh_job` |
| `extraction` | `{"documents", "tiers", "requested_by"}`, Domain set | `ExtractionRun.run` |
| `embeddings` | `{"branches", "reindex", "resume"}` optional, Domain set | `EmbeddingPass.run` |
| `audit_verify` | none | `lorebase_store.auth.AuditRepository.verify` over every chain |
| `audit_checkpoint` | none | `checkpoints.publish_checkpoints` |
| `vault.rotate` | `{"after": [domain id, token]}` optional | `lorebase_store.vault.rotate` |
| `reconcile_sweep` | `{"branch", "resume"}` optional, Domain set | `ReconcileSweep.run` |

An `extraction` job reads `documents` (ids) with `tiers` (a list, or null for
`scribe.extraction.tiers`); `requested_by` is recorded on the extraction job.

An `embeddings` job that reads `scribe.embeddings.max_records_per_job` rows before its pass is
complete queues its continuation (same payload plus `resume`) and succeeds.

`store.materialize` and `store.branch_delete` copy or delete a large branch's rows in batches
(`store.materialize.job_batch_rows` per transaction); at most `store.materialize.job_concurrency`
of them run at once across workers, and one that has to wait queues itself again.

`store.sensitivity_refresh` rewrites, in batches, the titles and search vectors a branch
materialized before the default branch tagged an Attribute (`materialize.stale`); one whose
branch stays locked queues its continuation (same branch plus `resume`).

An `audit_verify` job succeeds whether the chains verify or not: its result and the
`lorebase.audit.verifications` counter (outcome `intact` or `broken`, by stream) carry the
answer, and a broken chain is logged at error level. It verifies the chains of actions and,
under `reads`, those of the read stream.

An `audit_checkpoint` job signs the head of every chain of `scribe.audit_checkpoints.streams`
and stores one checkpoint per stream in the configured sink; `lorebase.audit.checkpoints`
counts them by outcome. A failure fails the attempt (it is retried like any job).

A `vault.rotate` job makes the vault rows of every Domain sealed or fingerprinted under
earlier keys again under the current ones (`vault.rekey_batch` rows per transaction); after
`vault.rekey_rows_per_job` rows it queues its continuation from where it stopped and succeeds.
Without vault keys it fails at once.

A `reconcile_sweep` job (design 10.6) that did `scribe.reconciliation.sweep_max_changes_per_job`
units of work before its pass is complete queues its continuation (same branch plus `resume`)
and succeeds.

Index DDL runs on `JobContext.ddl_engine` (the maintenance role) when one is configured.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_inference import InferenceSettings
from lorebase_scribe.checkpoints import CheckpointSink, build_sink, publish_checkpoints
from lorebase_scribe.embeddings import PERMANENT_ERRORS as EMBEDDING_ERRORS
from lorebase_scribe.embeddings import EmbeddingModels, EmbeddingPass, Templates
from lorebase_scribe.extraction.pipeline import PERMANENT_ERRORS as EXTRACTION_ERRORS
from lorebase_scribe.extraction.pipeline import ExtractionModels, ExtractionRun
from lorebase_scribe.reconcile.sweep import ReconcileSweep, SweepError
from lorebase_scribe.settings import ScribeSettings
from lorebase_scribe.sources import PERMANENT_ERRORS, SourceSync
from lorebase_scribe.sources.connectors import ConnectorFactory, default_connector
from lorebase_scribe.sources.sync import utc_now
from lorebase_scribe.telemetry import AUDIT_CHECKPOINTS, AUDIT_VERIFICATIONS
from lorebase_scribe.webhooks import HttpxSender
from lorebase_scribe.worker import JobHandler, PermanentJobError
from lorebase_store.auth import AuditRepository, AuditStream, Chains
from lorebase_store.embeddings import SchemaCache
from lorebase_store.gc import GcBusyError, GcError, run_gc_job
from lorebase_store.indexes import run_index_job
from lorebase_store.jobs import (
    BRANCH_DELETE_JOB,
    EMBEDDINGS_JOB,
    EXTRACTION_JOB,
    GC_JOB,
    INDEX_JOB,
    LINEAGE_BACKFILL_JOB,
    MATERIALIZE_JOB,
    RECONCILE_SWEEP_JOB,
    SENSITIVITY_REFRESH_JOB,
    SOURCE_SYNC_JOB,
    VAULT_ROTATE_JOB,
    JobRow,
    embeddings_payload,
    enqueue_job,
    reconcile_sweep_payload,
)
from lorebase_store.materialize import (
    run_branch_delete_job,
    run_lineage_backfill_job,
    run_materialize_job,
    run_sensitivity_refresh_job,
)
from lorebase_store.runtime import StoreRuntime
from lorebase_store.storage import constraints_only
from lorebase_store.vault import rotate
from lorebase_store.webhooks import WebhookSettings, deliver_pending

WEBHOOK_DELIVERY_JOB = "webhook_delivery"
AUDIT_VERIFY_JOB = "audit_verify"
AUDIT_CHECKPOINT_JOB = "audit_checkpoint"

log = logging.getLogger("lorebase.scribe.jobs")

JOB_TYPES = (
    SOURCE_SYNC_JOB,
    WEBHOOK_DELIVERY_JOB,
    INDEX_JOB,
    MATERIALIZE_JOB,
    BRANCH_DELETE_JOB,
    SENSITIVITY_REFRESH_JOB,
    LINEAGE_BACKFILL_JOB,
    GC_JOB,
    EXTRACTION_JOB,
    EMBEDDINGS_JOB,
    AUDIT_VERIFY_JOB,
    AUDIT_CHECKPOINT_JOB,
    VAULT_ROTATE_JOB,
    RECONCILE_SWEEP_JOB,
)
"""Every job type Scribe knows."""


@dataclass(frozen=True)
class JobContext:
    """What the handlers share: database, store runtime, settings and the HTTP client.

    `ddl_engine` runs per-Attribute index DDL: the maintenance engine
    (`database.maintenance_dsn`) when configured, else `engine`. `extraction_models` replaces
    the models built from `inference` (tests). `embedding_models` is the process's embedding
    model, created on first use from `inference` unless given; `embedding_templates` the
    process's cache of resolved embedding templates (one per pass when not given).
    `checkpoint_sink` replaces the sink of `scribe.audit_checkpoints` (tests)."""

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
    embedding_models: EmbeddingModels | None = None
    embedding_templates: SchemaCache[Templates] | None = None
    checkpoint_sink: CheckpointSink | None = None

    def embedding_pass(self) -> EmbeddingPass:
        """An embedding pass sharing the process's model."""
        models = self.embedding_models
        if models is None:
            raise RuntimeError("no embedding model: build the context with `embedding_models`")
        return EmbeddingPass(
            self.engine,
            self.sessions,
            models,
            self.settings.embeddings,
            dims=self.runtime.settings.embeddings.dims,
            clock=self.clock,
            templates=self.embedding_templates,
        )


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
            reconciliation=context.settings.reconciliation,
        )
        report = await sync.run(
            _domain(job), reference, dry_run=bool(job.payload.get("dry_run", False))
        )
        return report.as_dict()

    async def webhook_delivery(job: JobRow) -> Mapping[str, Any]:
        # The pass bounds its attempts itself; past the lease of its deliveries another pass
        # may send them again, so a pass still running then is abandoned (a failed attempt of
        # the job) instead of holding a worker slot that its heartbeat keeps from going stale.
        sender = HttpxSender(context.http, connect_timeout_s=context.webhooks.connect_timeout_s)
        lease_s = context.webhooks.lease_s
        budget = asyncio.timeout(lease_s)
        try:
            async with budget:
                report = await deliver_pending(
                    context.sessions,
                    context.webhooks,
                    sender,
                    writes=context.runtime.settings.writes,
                    outbox=context.runtime.settings.outbox,
                    vault=context.runtime.vault,
                )
        except TimeoutError:
            if not budget.expired():
                raise
            raise TimeoutError(f"the pass outlasted webhooks.lease_s ({lease_s:g}s)") from None
        return dataclasses.asdict(report)

    async def index(job: JobRow) -> Mapping[str, Any]:
        _domain(job)
        return await run_index_job(
            context.ddl_engine or context.engine,
            job,
            constraints_only=constraints_only(context.runtime.settings),
        )

    async def materialize(job: JobRow) -> Mapping[str, Any]:
        _domain(job)
        return await run_materialize_job(
            context.engine, job, runtime=context.runtime, clock=context.clock
        )

    async def branch_delete(job: JobRow) -> Mapping[str, Any]:
        _domain(job)
        return await run_branch_delete_job(
            context.engine, job, runtime=context.runtime, clock=context.clock
        )

    async def sensitivity_refresh(job: JobRow) -> Mapping[str, Any]:
        _domain(job)
        return await run_sensitivity_refresh_job(
            context.engine, job, runtime=context.runtime, clock=context.clock
        )

    async def lineage_backfill(job: JobRow) -> Mapping[str, Any]:
        return await run_lineage_backfill_job(context.engine, job, runtime=context.runtime)

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
            reconciliation=context.settings.reconciliation,
        )
        requested_by = job.payload.get("requested_by")
        report = await run.run(
            _domain(job),
            [str(d) for d in documents],
            tiers=None if tiers is None else [int(t) for t in tiers],
            requested_by=str(requested_by) if requested_by else None,
        )
        return report.as_dict()

    async def embeddings(job: JobRow) -> Mapping[str, Any]:
        domain = _domain(job)
        branches = job.payload.get("branches")
        if branches is not None and (
            not isinstance(branches, list) or not all(isinstance(b, str) for b in branches)
        ):
            raise PermanentJobError("`branches` must be a list of branch names")
        resume = job.payload.get("resume")
        if resume is not None and (
            not isinstance(resume, list)
            or len(resume) != 3
            or not all(isinstance(part, str) for part in resume)
        ):
            raise PermanentJobError("`resume` must be [branch, kind id, key]")
        reindex = bool(job.payload.get("reindex", False))
        report = await context.embedding_pass().run(
            domain,
            branches=branches,
            reindex=reindex,
            resume=None if resume is None else (resume[0], resume[1], resume[2]),
        )
        result = report.as_dict()
        if report.resume is not None:
            async with context.sessions() as session, session.begin():
                result["continuation"] = await enqueue_job(
                    session,
                    EMBEDDINGS_JOB,
                    domain_id=domain,
                    payload=embeddings_payload(
                        branches=branches, reindex=reindex, resume=report.resume
                    ),
                    unique=True,
                )
        return result

    async def verify_stream(stream: AuditStream) -> dict[str, Any]:
        async with context.sessions() as session, session.begin():
            result = await AuditRepository(session, stream).verify(domain_id=Chains.ALL)
        outcome = "intact" if result.ok else "broken"
        AUDIT_VERIFICATIONS.add(1, {"outcome": outcome, "stream": stream.value})
        report: dict[str, Any] = {
            "intact": result.ok,
            "checked": result.checked,
            "chains": result.chains,
            "last_seq": result.last_seq,
        }
        if not result.ok:
            report["first_broken"] = {
                "seq": result.bad_seq,
                "domain_id": result.bad_domain_id,
                "reason": result.reason,
            }
            log.error(
                "audit chain broken", extra={**report["first_broken"], "stream": stream.value}
            )
        return report

    async def audit_verify(job: JobRow) -> Mapping[str, Any]:
        report = await verify_stream(AuditStream.ACTIONS)
        report["reads"] = await verify_stream(AuditStream.READS)
        return report

    async def audit_checkpoint(job: JobRow) -> Mapping[str, Any]:
        settings = context.settings.audit_checkpoints
        if not settings.enabled:
            raise PermanentJobError("scribe.audit_checkpoints is not enabled")
        sink = context.checkpoint_sink or build_sink(settings, context.http)
        try:
            published = await publish_checkpoints(
                context.sessions, settings, sink, clock=context.clock
            )
        except Exception:
            AUDIT_CHECKPOINTS.add(1, {"outcome": "failed"})
            raise
        AUDIT_CHECKPOINTS.add(len(published), {"outcome": "published"})
        return {"published": [item.as_dict() for item in published]}

    async def vault_rotate(job: JobRow) -> Mapping[str, Any]:
        keys = context.runtime.vault
        if keys is None:
            raise PermanentJobError("vault.rotate needs vault.token_key and vault.encryption_key")
        after = job.payload.get("after")
        if after is not None and (
            not isinstance(after, list)
            or len(after) != 2
            or not all(isinstance(part, str) for part in after)
        ):
            raise PermanentJobError("`after` must be [domain id, token]")
        settings = context.runtime.vault_settings
        report = await rotate(
            context.sessions,
            keys,
            batch=settings.rekey_batch,
            max_rows=settings.rekey_rows_per_job,
            after=None if after is None else (after[0], after[1]),
        )
        result = report.as_dict()
        if report.after is not None:
            async with context.sessions() as session, session.begin():
                result["continuation"] = await enqueue_job(
                    session,
                    VAULT_ROTATE_JOB,
                    payload={"after": list(report.after)},
                    unique=True,
                )
        return result

    async def reconcile_sweep(job: JobRow) -> Mapping[str, Any]:
        domain = _domain(job)
        branch = job.payload.get("branch")
        if branch is not None and (not isinstance(branch, str) or not branch):
            raise PermanentJobError("`branch` must be a branch name")
        resume = job.payload.get("resume")
        if resume is not None and (
            not isinstance(resume, list) or not all(isinstance(part, str) for part in resume)
        ):
            raise PermanentJobError("`resume` must be a list of strings")
        sweep = ReconcileSweep(
            context.sessions,
            context.runtime,
            context.settings.reconciliation,
            principal=context.settings.worker.principal,
            clock=context.clock,
        )
        try:
            report = await sweep.run(domain, branch=branch, resume=resume)
        except SweepError as error:
            raise PermanentJobError(str(error)) from None
        result = report.as_dict()
        if report.resume is not None:
            async with context.sessions() as session, session.begin():
                result["continuation"] = await enqueue_job(
                    session,
                    RECONCILE_SWEEP_JOB,
                    domain_id=domain,
                    payload=reconcile_sweep_payload(branch=branch, resume=report.resume),
                    unique=True,
                )
        return result

    available: dict[str, JobHandler] = {
        SOURCE_SYNC_JOB: source_sync,
        WEBHOOK_DELIVERY_JOB: webhook_delivery,
        INDEX_JOB: index,
        MATERIALIZE_JOB: materialize,
        BRANCH_DELETE_JOB: branch_delete,
        SENSITIVITY_REFRESH_JOB: sensitivity_refresh,
        LINEAGE_BACKFILL_JOB: lineage_backfill,
        GC_JOB: gc,
        EXTRACTION_JOB: extraction,
        EMBEDDINGS_JOB: embeddings,
        AUDIT_VERIFY_JOB: audit_verify,
        AUDIT_CHECKPOINT_JOB: audit_checkpoint,
        VAULT_ROTATE_JOB: vault_rotate,
        RECONCILE_SWEEP_JOB: reconcile_sweep,
    }
    chosen = list(types) or list(available)
    unknown = sorted(set(chosen) - available.keys())
    if unknown:
        raise ValueError(f"unknown job type(s): {', '.join(unknown)}")
    return {name: available[name] for name in chosen}


PERMANENT_JOB_ERRORS: tuple[type[Exception], ...] = (
    *PERMANENT_ERRORS,
    *EXTRACTION_ERRORS,
    *EMBEDDING_ERRORS,
)
"""Handler failures the worker fails at once instead of retrying."""
