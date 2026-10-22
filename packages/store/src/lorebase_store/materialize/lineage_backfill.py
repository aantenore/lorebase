"""Lineage revisions of rows materialized before migration `0032`: their backfill in batches.

`mat_records.lineage_revisions` names the Revision behind each value of a Record and the one
that created it (`materialize.rows.next_lineage_revisions`). Rows written before the column
existed hold null, and rows of those Records updated since hold only the entries of their
later changes (no `$created`); readers fall back to the branch history for what is missing,
within `read.history.lineage_scan_changes` changes (Keeper `reading.record_history`).

**Backfill.** `backfill_lineage_revisions` folds the branch history of those rows as a rebuild
does (`materialize.rebuild.fold_history`) and writes the result, `materialize.job_batch_rows`
rows per transaction in `(Kind id, key)` order. A row is written only while it still holds
what the batch read, so a commit that changed it meanwhile wins; a Record with no history row
gets an empty map, so a batch run twice writes nothing new. Rebuilds and branch copies fold
the history themselves and leave nothing to backfill.

**Jobs.** `store.lineage_backfill` with payload `{}` (no Domain) is the sweep: it finds the
branches holding such rows, in one scan of `mat_records`, and queues the job with payload
`{"branch"}` for each in its Domain, which runs the backfill of that branch. A starting job
worker that runs the type queues the sweep once (`queue_lineage_sweep`): not again once one
is queued, running or done.

**Recompute.** Merges made before their changesets were restricted to the values they change
recorded the source's whole Lineage, so their Revision was named for values they left as they
were. With `"recompute": true` (in the sweep's payload, which then covers every branch with
materialized Records, or in a branch's), the job also folds again the rows whose lineage
revisions name a merge Revision; the fold attributes a merge step only the values that differ
from the branch's previous value (`rebuild.fold_history`). `python -m lorebase_store.jobs
lineage-backfill [--domain D --branch B] [--recompute]` queues it.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.tree import kind_partition
from lorebase_store.auth.scope import rls_scope
from lorebase_store.jobs import LINEAGE_BACKFILL_JOB, JobRow, enqueue_job
from lorebase_store.materialize.rebuild import fold_history
from lorebase_store.materialize.rows import CREATED_KEY
from lorebase_store.objectstore import PgObjectStore
from lorebase_store.repositories.materialized import MERGE_ORIGIN_TYPE, MaterializedRepository
from lorebase_store.repositories.scope import scope_to_domain
from lorebase_store.runtime import StoreRuntime, default_runtime

log = logging.getLogger(__name__)

SWEEP_PAYLOAD: Mapping[str, Any] = {}
"""Payload of the sweep job."""

_INCOMPLETE = f"(m.lineage_revisions IS NULL OR (m.lineage_revisions ->> '{CREATED_KEY}') IS NULL)"

_BRANCHES = text(
    f"""
    SELECT DISTINCT m.domain_id, m.branch FROM mat_records m
    WHERE NOT m.deleted AND {_INCOMPLETE}
    ORDER BY m.domain_id, m.branch
    """
)

# Serializes workers starting together, so they queue one sweep between them.
_SWEEP_LOCK = text("SELECT pg_advisory_xact_lock(hashtext('lorebase.store.lineage_backfill'))")

_SWEEP_SEEN = text(
    """
    SELECT 1 FROM jobs
    WHERE type = :type AND domain_id IS NULL AND payload = CAST(:payload AS jsonb)
      AND status::text IN ('queued', 'running', 'succeeded')
    LIMIT 1
    """
)

_ALL_BRANCHES = text(
    """
    SELECT DISTINCT m.domain_id, m.branch FROM mat_records m
    WHERE NOT m.deleted
    ORDER BY m.domain_id, m.branch
    """
)

# Rows whose lineage revisions name a merge Revision.
_NAMES_A_MERGE = f"""EXISTS (
    SELECT 1
    FROM jsonb_each_text(CASE WHEN jsonb_typeof(m.lineage_revisions) = 'object'
                              THEN m.lineage_revisions ELSE '{{}}'::jsonb END) AS e
    JOIN revisions r ON r.domain_id = m.domain_id AND r.id = decode(e.value, 'hex')
    WHERE r.origin ->> 'type' = '{MERGE_ORIGIN_TYPE}')"""


def _page(condition: str) -> TextClause:
    return text(
        f"""
    SELECT m.kind_id, m.key, m.lineage_revisions FROM mat_records m
    WHERE m.domain_id = :domain AND m.branch = :branch AND NOT m.deleted AND {condition}
      AND (m.kind_id, m.key) > (:kind, :key)
    ORDER BY m.kind_id, m.key
    LIMIT :limit
    """
    )


_PAGE = _page(_INCOMPLETE)
_RECOMPUTE_PAGE = _page(f"({_INCOMPLETE} OR {_NAMES_A_MERGE})")

_WRITE = text(
    """
    UPDATE mat_records m SET lineage_revisions = v.revs
    FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS v(kind_id text, key text, revs jsonb,
                                                        before jsonb)
    WHERE m.domain_id = :domain AND m.branch = :branch
      AND m.kind_id = v.kind_id AND m.key = v.key
      AND m.lineage_revisions IS NOT DISTINCT FROM v.before
    """
)


@dataclass
class LineageBackfill:
    """What a backfill did: rows read and rows written, in how many batches."""

    branch: str
    batches: int = 0
    rows: int = 0
    written: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "batches": self.batches,
            "rows": self.rows,
            "written": self.written,
        }


def backfill_payload(branch: str | None = None, *, recompute: bool = False) -> dict[str, Any]:
    """Payload of a `store.lineage_backfill` job: the sweep without `branch`."""
    payload: dict[str, Any] = {} if branch is None else {"branch": branch}
    if recompute:
        payload["recompute"] = True
    return payload


async def queue_lineage_sweep(sessions: async_sessionmaker[AsyncSession]) -> int | None:
    """Queue the sweep unless one is queued, running or done; returns its id when queued.
    Workers starting together wait for one another (a transaction advisory lock)."""
    payload = json.dumps(dict(SWEEP_PAYLOAD))
    async with sessions() as session, session.begin():
        await session.execute(_SWEEP_LOCK)
        async with rls_scope(session, bypass=True):
            seen = await session.execute(
                _SWEEP_SEEN, {"type": LINEAGE_BACKFILL_JOB, "payload": payload}
            )
            if seen.first() is not None:
                return None
        return await enqueue_job(session, LINEAGE_BACKFILL_JOB, payload=SWEEP_PAYLOAD)


async def queue_branch_backfills(session: AsyncSession, *, recompute: bool = False) -> list[int]:
    """Queue the backfill of every branch holding rows without full lineage revisions, or
    with `recompute` of every branch with materialized Records (an identical job not started
    yet is reused)."""
    async with rls_scope(session, bypass=True):
        found = (await session.execute(_ALL_BRANCHES if recompute else _BRANCHES)).all()
    return [
        await enqueue_job(
            session,
            LINEAGE_BACKFILL_JOB,
            domain_id=domain_id,
            payload=backfill_payload(branch, recompute=recompute),
            unique_queued=True,
        )
        for domain_id, branch in found
    ]


async def backfill_lineage_revisions(
    engine: AsyncEngine,
    domain_id: str,
    branch: str,
    *,
    runtime: StoreRuntime,
    recompute: bool = False,
) -> LineageBackfill:
    """Write the lineage revisions of the branch's rows that lack them (with `recompute`,
    also of those naming a merge Revision), in batches, each in its own transaction (module
    doc)."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    report = LineageBackfill(branch)
    cursor = ("", "")
    while True:
        async with sessions() as session, session.begin():
            last = await _backfill_batch(
                session, domain_id, branch, cursor, runtime, report, recompute=recompute
            )
        if last is None:
            return report
        cursor = last


async def _backfill_batch(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    cursor: tuple[str, str],
    runtime: StoreRuntime,
    report: LineageBackfill,
    *,
    recompute: bool,
) -> tuple[str, str] | None:
    settings = runtime.settings
    await scope_to_domain(session, domain_id)
    params = {
        "domain": domain_id,
        "branch": branch,
        "kind": cursor[0],
        "key": cursor[1],
        "limit": settings.materialize.job_batch_rows,
    }
    page = (await session.execute(_RECOMPUTE_PAGE if recompute else _PAGE, params)).all()
    if not page:
        return None
    repository = MaterializedRepository(session, domain_id, branch, writes=settings.writes)
    objects = PgObjectStore(
        session, domain_id, writes=settings.writes, batch=settings.materialize.read_batch_rows
    )
    by_kind: dict[str, list[tuple[str, dict[str, Any] | None]]] = defaultdict(list)
    for kind_id, key, before in page:
        by_kind[kind_id].append((key, before))
    rows: list[dict[str, Any]] = []
    for kind_id, entries in by_kind.items():
        folded = await fold_history(
            repository,
            kind_partition(kind_id),
            batch=settings.materialize.read_batch_rows,
            objects=objects,
            keys=[key for key, _ in entries],
        )
        for key, before in entries:
            found = folded.get(key)
            revisions = {} if found is None else dict(sorted(found.revisions.items()))
            if before is not None and not revisions:
                continue
            if revisions != before:
                rows.append({"kind_id": kind_id, "key": key, "revs": revisions, "before": before})
    if rows:
        result = await session.execute(
            _WRITE, {"domain": domain_id, "branch": branch, "rows": json.dumps(rows)}
        )
        report.written += int(getattr(result, "rowcount", 0) or 0)
    report.batches += 1
    report.rows += len(page)
    return str(page[-1][0]), str(page[-1][1])


async def run_lineage_backfill_job(
    engine: AsyncEngine, job: JobRow, *, runtime: StoreRuntime | None = None
) -> Mapping[str, Any]:
    """Job handler of `store.lineage_backfill` (module doc)."""
    runtime = runtime or default_runtime()
    branch = job.payload.get("branch")
    recompute = job.payload.get("recompute", False)
    if not isinstance(recompute, bool):
        raise ValueError("`recompute` must be a boolean")
    if branch is None and job.domain_id is None:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session, session.begin():
            queued = await queue_branch_backfills(session, recompute=recompute)
        return {"queued": queued, "recompute": recompute}
    if job.domain_id is None or not isinstance(branch, str) or not branch:
        raise ValueError(f"a {job.type} job needs a Domain and a `branch`, or neither")
    report = await backfill_lineage_revisions(
        engine, job.domain_id, branch, runtime=runtime, recompute=recompute
    )
    log.info("lineage revisions backfilled", extra=report.as_dict())
    return {**report.as_dict(), "recompute": recompute}


__all__ = [
    "SWEEP_PAYLOAD",
    "LineageBackfill",
    "backfill_lineage_revisions",
    "backfill_payload",
    "queue_branch_backfills",
    "queue_lineage_sweep",
    "run_lineage_backfill_job",
]
