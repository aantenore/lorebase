"""Titles and search vectors a branch materialized before the default branch tagged an
Attribute (design 10.2): their rewrite in batches.

A branch forked before the default branch tagged a title or searchable Attribute keeps the
titles and search vectors it computed without the tag: Keeper keeps them out of responses and
lexical search for callers without the clearance, and verification reports the titles as
`sensitivity_stale`. The materializer adds the default branch's tags whenever it writes a
Record (`materialize.sensitivity`), so only rows written before the tag are stale, in the
Kinds whose title or search weights the tags change (`stale_kinds`).

**Rewrite.** `refresh_stale_rows` recomputes the titles and search vectors of those Kinds as
the materializer computes them now, `materialize.job_batch_rows` Records per transaction in
`(Kind id, key)` order. Each batch holds the branch lock, like a commit, and writes through
`MaterializedRepository` (rows an overlay reads from its base are copied up, rows overlays
based on the branch read are copied into them first, stamped with the generation of the
branch head). Only titles that differ are written, so a batch run twice writes nothing new.
Nothing is committed: titles and search vectors are not part of the tree.

Tags are the effective ones (`materialize.sensitivity.tagged_schema`): a derived title or
searchable Attribute reading a tagged Attribute is stale too.

**Job.** `store.sensitivity_refresh` (payload `{"branch"}`) runs the rewrite for one branch.
A batch that waits longer than `commit.lock_timeout_ms` for the branch lock, three times, queues
the job again after `materialize.job_retry_seconds`, with `resume` set to the last key
written. When it is done, the job queues itself for the overlays based on the branch: the
copies it made into them hold the old rows, and their own rows are theirs to rewrite. A job
is queued unless an identical one is still queued; a running one does not count, since it
may already be past the Kinds a new tag makes stale.

**Automatic.** With `materialize.refresh_branches_on_tag`, a commit or merge on the default
branch that adds a tag (to any Attribute, new ones included) queues the job, in one statement
of its transaction, for every other branch that has stale Kinds by its schema, except
overlays whose base is queued (the base's job queues them). Creating a copy of a branch with
stale Kinds queues the copy (an overlay's source is queued instead), and so does the end of a
background copy (`queue_if_stale`). The branches' schemas are not changed:
`POST .../branches/{b}/rebase-tags` (Keeper) commits the tags to a branch's schema, and that
schema change recomputes the titles and search vectors of the Kinds it changes in its own
transaction, as every schema change does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.expr.sensitivity import raised_tags, schema_tags
from lorebase_core.model import Kind, Schema
from lorebase_store.auth.scope import rls_scope
from lorebase_store.commit.schema import load_compiled
from lorebase_store.jobs import SENSITIVITY_REFRESH_JOB, JobRow, enqueue_job
from lorebase_store.materialize.rows import record_title, search_weights
from lorebase_store.materialize.sensitivity import branch_tagged_schema, tagged_schema
from lorebase_store.models.enums import Materialization, RefKind
from lorebase_store.repositories.domains import DomainRepository
from lorebase_store.repositories.materialized import MaterializedRepository
from lorebase_store.repositories.refs import LockTimeoutError, RefRepository, sqlstate
from lorebase_store.repositories.revisions import RevisionRepository
from lorebase_store.repositories.scope import scope_to_domain
from lorebase_store.runtime import StoreRuntime, default_runtime
from lorebase_store.telemetry import BRANCH_JOB_BATCH_DURATION, BRANCH_JOB_ROWS, BRANCH_JOB_RUNS

log = logging.getLogger(__name__)

JOB_NAME = "sensitivity_refresh"
"""Value of the `job` attribute of the branch job metrics."""
_RETRYABLE = frozenset({"40P01", "40001"})
_ATTEMPTS = 3
"""Runs of a batch that hits a retryable failure or a busy branch before the job queues
itself again."""

Cursor = tuple[str, str]
"""`(Kind id, key)` of the last Record a batch read."""


def _now() -> datetime:
    return datetime.now(UTC)


def _title_hidden(kind: Kind) -> bool:
    attribute = kind.attributes.get(kind.title_attribute or "")
    return attribute is not None and attribute.is_sensitive


def stale_kinds(own: Schema, tagged: Schema) -> list[str]:
    """Kinds of `own` whose titles or search vectors `tagged` (`own` with the default branch's
    tags added) computes differently: a title or searchable Attribute it tags."""
    stale = []
    for kind_id, kind in own.kinds.items():
        other = tagged.kinds.get(kind_id)
        if other is None:
            continue
        if _title_hidden(other) != _title_hidden(kind) or search_weights(other) != search_weights(
            kind
        ):
            stale.append(kind_id)
    return sorted(stale)


@dataclass
class StaleRefresh:
    """What a rewrite did: the stale Kinds, Records read, titles rewritten, and where it
    stopped when it was deferred (`resume`)."""

    branch: str
    status: str = "done"
    kinds: list[str] = field(default_factory=list)
    batches: int = 0
    rows: int = 0
    titles: int = 0
    vectors: int = 0
    resume: Cursor | None = None

    @property
    def rewrote(self) -> bool:
        return bool(self.titles or self.vectors)

    def as_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "status": self.status,
            "kinds": list(self.kinds),
            "batches": self.batches,
            "rows": self.rows,
            "titles": self.titles,
            "vectors": self.vectors,
            "resume": None if self.resume is None else list(self.resume),
        }


def refresh_payload(branch: str, resume: Cursor | None = None) -> dict[str, Any]:
    """Payload of a `store.sensitivity_refresh` job."""
    payload: dict[str, Any] = {"branch": branch}
    if resume is not None:
        payload["resume"] = list(resume)
    return payload


async def queue_stale_refresh(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    *,
    resume: Cursor | None = None,
    run_after: datetime | None = None,
) -> int:
    """Queue the rewrite of a branch's stale rows (an identical job that has not started yet
    is reused)."""
    return await enqueue_job(
        session,
        SENSITIVITY_REFRESH_JOB,
        domain_id=domain_id,
        payload=refresh_payload(branch, resume),
        run_after=run_after,
        unique_queued=True,
    )


async def queue_if_stale(
    session: AsyncSession, domain_id: str, branch: str, schema: Schema, *, runtime: StoreRuntime
) -> int | None:
    """With `materialize.refresh_branches_on_tag`, queue the rewrite of `branch` (whose
    schema is `schema`) when the default branch's tags make some of its Kinds stale."""
    if not runtime.settings.materialize.refresh_branches_on_tag:
        return None
    tagged = await branch_tagged_schema(session, domain_id, branch, schema, runtime=runtime)
    if not stale_kinds(schema, tagged):
        return None
    return await queue_stale_refresh(session, domain_id, branch)


_BRANCH_SCHEMAS = text(
    """
    SELECT r.name, r.materialization::text, r.base_branch, s.schema_hash
    FROM refs r JOIN mat_schema s ON s.domain_id = r.domain_id AND s.branch = r.name
    WHERE r.domain_id = :domain AND r.kind = 'branch' AND r.deleted_at IS NULL
      AND r.name <> :default
    """
)
_COMPILED = text(
    """
    SELECT DISTINCT ON (schema_hash) schema_hash, compiled FROM mat_schema
    WHERE domain_id = :domain AND schema_hash = ANY(CAST(:hashes AS bytea[]))
    """
)
_ENQUEUE_MISSING = text(
    """
    INSERT INTO jobs (type, domain_id, payload, priority, run_after)
    SELECT CAST(:type AS text), CAST(:domain AS text), e.payload, 0, CAST(:now AS timestamptz)
    FROM jsonb_array_elements(CAST(:payloads AS jsonb)) AS e(payload)
    WHERE NOT EXISTS (
        SELECT 1 FROM jobs j
        WHERE j.type = CAST(:type AS text) AND j.domain_id = CAST(:domain AS text)
          AND j.status::text = 'queued' AND j.payload = e.payload
    )
    RETURNING id
    """
)


async def queue_after_tagging(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    old: Schema,
    new: Schema,
    *,
    runtime: StoreRuntime,
) -> list[int]:
    """In the transaction of a commit or merge that changed the schema of `branch` from `old`
    to `new`: when `branch` is the default branch, `new` adds tags and
    `materialize.refresh_branches_on_tag` is on, queue the rewrite, in one statement, for
    every other branch that has stale Kinds by its schema and `new`'s tags, except overlays
    whose base is queued too. Returns the job ids."""
    if not runtime.settings.materialize.refresh_branches_on_tag or not raised_tags(old, new):
        return []
    domain = await DomainRepository(session).get(domain_id)
    if domain is None or branch != domain.default_branch:
        return []
    found = (await session.execute(_BRANCH_SCHEMAS, {"domain": domain_id, "default": branch})).all()
    hashes = sorted({bytes(row[3]) for row in found})
    compiled = {
        bytes(digest): load_compiled(body)
        for digest, body in (
            await session.execute(_COMPILED, {"domain": domain_id, "hashes": hashes})
        ).all()
    }
    reference = schema_tags(new)
    limits = runtime.schema.expression_limits
    stale_by_hash = {
        digest: bool(stale_kinds(schema, tagged_schema(schema, reference, limits)))
        for digest, schema in compiled.items()
    }
    stale = {name for name, _, _, digest in found if stale_by_hash.get(bytes(digest))}
    queued = sorted(
        name
        for name, materialization, base, _ in found
        if name in stale
        and not (materialization == Materialization.OVERLAY.value and base in stale)
    )
    if not queued:
        return []
    payloads = json.dumps([refresh_payload(name) for name in queued])
    async with rls_scope(session, bypass=True):
        result = await session.execute(
            _ENQUEUE_MISSING,
            {
                "type": SENSITIVITY_REFRESH_JOB,
                "domain": domain_id,
                "payloads": payloads,
                "now": _now(),
            },
        )
    return [int(job_id) for (job_id,) in result.all()]


async def refresh_stale_rows(
    engine: AsyncEngine,
    domain_id: str,
    branch: str,
    *,
    runtime: StoreRuntime,
    resume: Cursor | None = None,
) -> StaleRefresh:
    """Rewrite the stale titles and search vectors of a branch in batches, each in its own
    transaction (module doc), from `resume` when given. The report's status is `done`,
    `deferred` (the branch stayed locked; `resume` says where to go on) or `cancelled` (the
    branch is gone or not materialized)."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = runtime.settings
    report = StaleRefresh(branch)
    cursor = resume
    failures = 0
    while True:
        started = time.perf_counter()
        try:
            async with sessions() as session, session.begin():
                batch = await _refresh_batch(session, domain_id, branch, cursor, runtime, report)
        except LockTimeoutError:
            failures += 1
            if failures >= _ATTEMPTS:
                report.status, report.resume = "deferred", cursor
                return report
            await asyncio.sleep(min(settings.materialize.job_retry_seconds, 1.0))
            continue
        except DBAPIError as error:
            failures += 1
            if sqlstate(error) not in _RETRYABLE or failures >= _ATTEMPTS:
                raise
            log.info("sensitivity refresh of branch %s: batch retried after %s", branch, error)
            continue
        BRANCH_JOB_BATCH_DURATION.record(time.perf_counter() - started, {"job": JOB_NAME})
        failures = 0
        if batch is None:
            return report
        cursor = batch


async def _refresh_batch(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    cursor: Cursor | None,
    runtime: StoreRuntime,
    report: StaleRefresh,
) -> Cursor | None:
    """Rewrite the next batch after `cursor`; returns the new cursor, None when done."""
    settings = runtime.settings
    await scope_to_domain(session, domain_id)
    refs = RefRepository(session, domain_id)
    await refs.lock(branch, timeout_ms=settings.commit.lock_timeout_ms)
    ref = await refs.get(branch)
    if ref is None or ref.kind is not RefKind.BRANCH:
        report.status = "cancelled"
        return None
    generation = (await RevisionRepository(session, domain_id).generations([ref.head]))[ref.head]
    repository = MaterializedRepository(
        session,
        domain_id,
        branch,
        writes=settings.writes,
        generation=generation,
        lock_timeout_ms=settings.commit.lock_timeout_ms,
    )
    stored = await repository.schema()
    if stored is None:
        report.status = "cancelled"
        return None
    own = load_compiled(stored.compiled)
    tagged = await branch_tagged_schema(session, domain_id, branch, own, runtime=runtime)
    report.kinds = stale_kinds(own, tagged)
    for kind_id in report.kinds:
        if cursor is not None and kind_id < cursor[0]:
            continue
        after = cursor[1] if cursor is not None and cursor[0] == kind_id else None
        page = await repository.record_page(
            kind_id, after=after, limit=settings.materialize.job_batch_rows
        )
        if not page:
            continue
        kind = tagged.kinds[kind_id]
        titles = [
            (row.key, title)
            for row in page
            if (title := record_title(kind, row.values)) != row.title
        ]
        if titles:
            await repository.update_titles(kind_id, titles)
        weights = search_weights(kind)
        config = settings.materialize.text_search_config
        # Vectors hold the title at weight A: a changed title changes the vector too. Other
        # vectors are rewritten only when they differ from what the weights compute now.
        keys = {key for key, _ in titles}
        if weights != search_weights(own.kinds[kind_id]):
            others = [row.key for row in page if row.key not in keys]
            keys.update(
                await repository.stale_search_keys(kind_id, weights, config=config, keys=others)
            )
        if keys:
            await repository.refresh_search(kind_id, weights, config=config, keys=sorted(keys))
        report.batches += 1
        report.rows += len(page)
        report.titles += len(titles)
        report.vectors += len(keys)
        BRANCH_JOB_ROWS.add(len(page), {"job": JOB_NAME})
        return kind_id, page[-1].key
    return None


async def run_sensitivity_refresh_job(
    engine: AsyncEngine,
    job: JobRow,
    *,
    runtime: StoreRuntime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Mapping[str, Any]:
    """Job handler of `store.sensitivity_refresh` (module doc)."""
    runtime = runtime or default_runtime()
    now = clock or _now
    if job.domain_id is None:
        raise ValueError(f"a {job.type} job needs a Domain")
    branch = job.payload.get("branch")
    if not isinstance(branch, str) or not branch:
        raise ValueError(f"a {job.type} job needs a `branch`")
    resume = _cursor(job.payload.get("resume"))
    try:
        report = await refresh_stale_rows(
            engine, job.domain_id, branch, runtime=runtime, resume=resume
        )
    except Exception:
        BRANCH_JOB_RUNS.add(1, {"job": JOB_NAME, "outcome": "failed"})
        raise
    BRANCH_JOB_RUNS.add(1, {"job": JOB_NAME, "outcome": report.status})
    result = report.as_dict()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    if report.status == "deferred":
        delay = timedelta(seconds=runtime.settings.materialize.job_retry_seconds)
        async with sessions() as session, session.begin():
            result["again"] = await queue_stale_refresh(
                session, job.domain_id, branch, resume=report.resume, run_after=now() + delay
            )
    elif report.status == "done":
        async with sessions() as session, session.begin():
            await scope_to_domain(session, job.domain_id)
            overlays = await RefRepository(session, job.domain_id).overlays_of(branch)
            result["overlays"] = [
                await queue_stale_refresh(session, job.domain_id, ref.name) for ref in overlays
            ]
    return result


def _cursor(value: Any) -> Cursor | None:
    if value is None:
        return None
    if (
        not isinstance(value, Sequence)
        or isinstance(value, str)
        or len(value) != 2
        or not all(isinstance(part, str) for part in value)
    ):
        raise ValueError("`resume` must be [kind id, key]")
    return value[0], value[1]


__all__ = [
    "JOB_NAME",
    "StaleRefresh",
    "queue_after_tagging",
    "queue_if_stale",
    "queue_stale_refresh",
    "refresh_payload",
    "refresh_stale_rows",
    "run_sensitivity_refresh_job",
    "stale_kinds",
]
