"""Large branches in the background: copies, builds and deletions in batches (store schema
notes, "Large branches").

**Copy.** A copy branch whose source holds more than `store.materialize.async_copy_min_rows`
rows is created as an overlay of its source (`create_branch`): readable and writable at once,
with the semantics of an overlay. `queue_materialization` records its progress row
(`branch_materializations`, status `pending`, strategy `base`) and queues a
`store.materialize` job, which `run_materialize_job` runs in batches, each in its own
transaction:

1. `records`, `links`, `history`: copy the next base rows the overlay reads (revision
   generation at or before its fork point; history of Revisions up to it) in primary key
   order, from the cursor saved by the previous batch, with `INSERT ... ON CONFLICT DO
   NOTHING`.
2. `switch`: drop the overlay's tombstones, set `materialization` to `full` (head and version
   unchanged) and queue the per-Attribute indexes of a full branch.
3. When the overlay's schema changed during the copy (`apply.apply_changeset` records it in
   `reconcile` instead of refusing the change), `prune`, `derived`, `paths` and `search`
   recompute in batches what a schema change recomputes on a full branch (`build`).

Every batch holds the branch lock, so commits on the branch and copies on write from its base
wait for at most one batch (their inserts of the same keys could otherwise deadlock with it).
Batches are sized by the time they hold that lock (`_Pace`): from `job_batch_min_rows` up to
`job_batch_rows`, aiming at `job_batch_target_ms`, or at `job_batch_contended_ms` once a
commit waited for the lock. Between batches commits run freely, and the copy stays correct
because a copied row is the row the overlay already reads, and every row it shadows is kept:
- base rows written at or before the fork never change afterwards: a base commit that changes
  or removes one first copies it into the overlay (copy on write), and stamps what it writes
  with a later generation, which the copy skips;
- a commit on the branch first copies a base row up, then changes it, and a delete leaves a
  tombstone: a batch that meets such a row finds it held and keeps it (`DO NOTHING`), and a
  commit that meets a row a batch copied changes that copy.
So reads see the same rows before, during and after the copy (the overlay's rows win over the
base's, history included), and a batch run twice (a retry, a resumed job) copies nothing new.
A promotion, re-fork or rebuild, which delete rows of the branch, hold the copy fence
(`copy_fence`) against batches; a re-fork (another fork point) restarts the copy from the first
row. A batch that waits longer than `commit.lock_timeout_ms` for the branch lock, three times,
queues the job again after `job_retry_seconds`.

**Build.** A branch whose rows cannot come from an overlay's base (a large copy of an overlay
or of an older Revision, an overlay detached from its base) is `building`: its rows are
written from the tree of its head (strategy `tree`: `clear`, `tree_history`, `tree_rows`,
`derived`, `paths`, then `switch` to `full`). Meanwhile reads of its head are served from the
tree, and commits fail with `BranchMaterializingError` (retry when it is done).

**Failures.** A copy whose job fails `store.jobs.max_attempts` times is queued again after a
delay doubling from `job_retry_seconds` (at most `copy_retry_max_seconds`); after
`copy_max_failures` failed jobs, an overlay is detached from its base and built from its tree
instead (outcome `fallback`), so it no longer holds up the deletion of its base. A build that
fails as often is marked `failed` (outcome `given_up`); queuing it again resumes it.

**Delete.** `delete_branch` deletes a branch holding at most `async_delete_min_rows` rows in
the caller's transaction (promoting overlays based on it first). A larger one is hidden at once
(`refs.deleted_at`): reads, listings and commits no longer find it, its queued index builds and
copies are cancelled, and a `store.branch_delete` job deletes its rows in batches, then the
ref. The name stays taken until then. Overlays based on it keep reading its rows: their copies
are queued, and the deletion waits for them before it deletes a row.

At most `job_concurrency` of these jobs run at once across workers (session advisory locks);
a job that finds every slot taken, or that waits, queues itself again after
`job_retry_seconds` and succeeds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import Table, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.tree import PARTITION_SCHEMA
from lorebase_store.indexes import BranchIndexes, sql_literal
from lorebase_store.jobs import BRANCH_DELETE_JOB, MATERIALIZE_JOB, JobRow, enqueue_job
from lorebase_store.layout import MAT_HISTORY, MAT_LINKS, MAT_RECORDS
from lorebase_store.materialize import build
from lorebase_store.materialize.overlay import promote_overlays_of
from lorebase_store.materialize.stale import queue_if_stale
from lorebase_store.models.enums import Materialization
from lorebase_store.nodestore import NodeRepository
from lorebase_store.repositories.materializations import (
    BranchMaterializationRepository,
    MaterializationPhase,
    MaterializationState,
    MaterializationStatus,
    MaterializationStrategy,
    copy_fence,
)
from lorebase_store.repositories.materialized import MaterializedRepository
from lorebase_store.repositories.refs import (
    LockTimeoutError,
    RefRepository,
    RefRow,
    advisory_lock_key,
    sqlstate,
)
from lorebase_store.repositories.revisions import RevisionRepository, RevisionRow
from lorebase_store.repositories.scope import scope_to_domain
from lorebase_store.runtime import StoreRuntime, default_runtime
from lorebase_store.settings import MaterializeSettings
from lorebase_store.storage import constraints_only
from lorebase_store.telemetry import (
    BRANCH_JOB_BATCH_DURATION,
    BRANCH_JOB_BATCH_ROWS,
    BRANCH_JOB_ROWS,
    BRANCH_JOB_RUNS,
)

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]

_RETRYABLE = frozenset({"40P01", "40001"})
"""Deadlock and serialization failures: the batch is run again."""
_ATTEMPTS = 3
"""Runs of a batch that hits a retryable failure, or of a switch that finds the branch busy,
before the job queues itself again."""
_CONTENDED_FRACTION = 10
"""A batch that waited for the branch lock longer than `job_batch_contended_ms` divided by
this found a commit holding it (an uncontended lock is taken in well under that)."""


def _now() -> datetime:
    return datetime.now(UTC)


class BranchDeletion(StrEnum):
    """How `delete_branch` deleted a branch."""

    DELETED = "deleted"
    """Ref and rows are gone."""
    SCHEDULED = "scheduled"
    """The branch is hidden; a `store.branch_delete` job deletes its rows."""


@dataclass(frozen=True, slots=True)
class _Copied:
    """A table a background copy reads, in primary key order after (domain, branch)."""

    phase: MaterializationPhase
    table: Table
    key: tuple[str, ...]


_COPIED = (
    _Copied(MaterializationPhase.RECORDS, MAT_RECORDS, ("kind_id", "key")),
    _Copied(MaterializationPhase.LINKS, MAT_LINKS, ("relation_id", "from_key", "to_key")),
    _Copied(MaterializationPhase.HISTORY, MAT_HISTORY, ("partition", "element_key", "revision_id")),
)
_NEXT = {
    MaterializationPhase.RECORDS: MaterializationPhase.LINKS,
    MaterializationPhase.LINKS: MaterializationPhase.HISTORY,
    MaterializationPhase.HISTORY: MaterializationPhase.SWITCH,
}
_NEXT_BUILD = {
    MaterializationPhase.CLEAR: MaterializationPhase.TREE_HISTORY,
    MaterializationPhase.TREE_HISTORY: MaterializationPhase.TREE_ROWS,
    MaterializationPhase.TREE_ROWS: MaterializationPhase.DERIVED,
    MaterializationPhase.DERIVED: MaterializationPhase.PATHS,
    MaterializationPhase.PATHS: MaterializationPhase.SWITCH,
}
_NEXT_RECONCILE = {
    MaterializationPhase.PRUNE: MaterializationPhase.DERIVED,
    MaterializationPhase.DERIVED: MaterializationPhase.PATHS,
    MaterializationPhase.PATHS: MaterializationPhase.SEARCH,
    MaterializationPhase.SEARCH: MaterializationPhase.DONE,
}


# Queueing ---------------------------------------------------------------------------------------


async def queue_materialization(
    session: AsyncSession, domain_id: str, ref: RefRow, *, at: datetime
) -> MaterializationState:
    """Queue the background copy of the overlay `ref` (in the caller's transaction): its
    progress row starts `pending` (a failed copy of the same fork point keeps its cursor)
    unless a copy is already queued or running, and a `store.materialize` job is queued unless
    one is."""
    if ref.base_branch is None or ref.base_generation is None:
        raise ValueError(f"branch {ref.name!r} is not an overlay")
    states = BranchMaterializationRepository(session, domain_id)
    state = await states.get(ref.name, for_update=True)
    same_fork = (
        state is not None
        and state.strategy is MaterializationStrategy.BASE
        and (state.base_branch, state.base_generation) == (ref.base_branch, ref.base_generation)
    )
    if state is not None and same_fork and state.status is MaterializationStatus.FAILED:
        await states.update(ref.name, at=at, status=MaterializationStatus.PENDING)
        state = await states.get(ref.name)
        assert state is not None
    elif state is None or not same_fork or not state.active:
        state = await states.start(
            ref.name,
            base_branch=ref.base_branch,
            base_generation=ref.base_generation,
            status=MaterializationStatus.PENDING,
            at=at,
        )
    await enqueue_job(
        session, MATERIALIZE_JOB, domain_id=domain_id, payload={"branch": ref.name}, unique=True
    )
    return state


async def queue_build(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    *,
    source: str,
    head: RevisionRow,
    at: datetime,
    failures: int = 0,
    unique: bool = True,
) -> MaterializationState:
    """Queue the build of the `building` branch `branch` from the tree of its head (in the
    caller's transaction); `source` is recorded as where it came from."""
    state = await BranchMaterializationRepository(session, domain_id).start(
        branch,
        base_branch=source,
        base_generation=head.generation,
        status=MaterializationStatus.PENDING,
        strategy=MaterializationStrategy.TREE,
        failures=failures,
        at=at,
    )
    await enqueue_job(
        session, MATERIALIZE_JOB, domain_id=domain_id, payload={"branch": branch}, unique=unique
    )
    return state


async def detach_to_build(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    *,
    runtime: StoreRuntime,
    at: datetime,
    failures: int = 0,
    unique: bool = True,
) -> bool:
    """Detach the overlay `branch` from its base, in the caller's transaction: it becomes
    `building` (no base, head and version unchanged) and a job writes its rows from the tree
    of its head (`queue_build`). Its base no longer waits for it. Takes the branch lock (after
    the base's, when the caller holds it) and the copy fence. `False` when it is not an
    overlay."""
    refs = RefRepository(session, domain_id)
    await refs.lock(branch, timeout_ms=runtime.settings.commit.lock_timeout_ms)
    await copy_fence(session, domain_id, branch)
    ref = await refs.get(branch)
    if ref is None or ref.materialization is not Materialization.OVERLAY:
        return False
    head = await RevisionRepository(session, domain_id).require(ref.head)
    await refs.set_materialization(branch, Materialization.BUILDING)
    await queue_build(
        session,
        domain_id,
        branch,
        source=ref.base_branch or branch,
        head=head,
        at=at,
        failures=failures,
        unique=unique,
    )
    log.warning(
        "overlay branch %s of Domain %s detached from its base %s; it is rebuilt from its tree",
        branch,
        domain_id,
        ref.base_branch,
    )
    return True


async def delete_branch(
    session: AsyncSession,
    domain_id: str,
    name: str,
    *,
    runtime: StoreRuntime | None = None,
    clock: Clock | None = None,
) -> BranchDeletion:
    """Delete branch `name` (see the module doc) in the caller's transaction, after taking its
    lock. Raises `RefNotFoundError` when it does not exist (or is already being deleted)."""
    runtime = runtime or default_runtime()
    now = (clock or _now)()
    settings = runtime.settings.materialize
    refs = RefRepository(session, domain_id)
    await refs.lock(name, timeout_ms=runtime.settings.commit.lock_timeout_ms)
    ref = await refs.require(name)
    rows = MaterializedRepository(
        session, domain_id, name, writes=runtime.settings.writes, layout=ref.layout
    )
    if await rows.own_rows(limit=settings.async_delete_min_rows) <= settings.async_delete_min_rows:
        await promote_overlays_of(session, domain_id, name, runtime=runtime)
        await refs.delete(name)
        return BranchDeletion.DELETED
    overlays = await refs.overlays_of(name)
    await refs.mark_deleted(name, at=now)
    for overlay in overlays:
        await queue_materialization(session, domain_id, overlay, at=now)
    await enqueue_job(
        session, BRANCH_DELETE_JOB, domain_id=domain_id, payload={"branch": name}, unique=True
    )
    log.info(
        "branch %s of Domain %s hidden; its rows are deleted in the background", name, domain_id
    )
    return BranchDeletion.SCHEDULED


# Jobs -------------------------------------------------------------------------------------------


@dataclass
class _Pace:
    """Rows of the next batch, adapted to the time batches hold the branch lock: halved
    toward the target when a batch held it too long, doubled when it held it for less than
    half the target. The target is `job_batch_contended_ms` after a commit waited for the
    lock (before or during the batch), `job_batch_target_ms` otherwise."""

    low: int
    high: int
    target_s: float
    contended_s: float
    size: int

    @classmethod
    def of(cls, settings: MaterializeSettings) -> _Pace:
        low = min(settings.job_batch_min_rows, settings.job_batch_rows)
        return cls(
            low=low,
            high=settings.job_batch_rows,
            target_s=settings.job_batch_target_ms / 1000,
            contended_s=settings.job_batch_contended_ms / 1000,
            size=low,
        )

    def contended(self, waited_s: float, waiters: int) -> bool:
        return waiters > 0 or waited_s > self.contended_s / _CONTENDED_FRACTION

    def next(self, held_s: float, *, contended: bool) -> None:
        aim = self.contended_s if contended else self.target_s
        if held_s > aim:
            self.size = max(self.low, int(self.size * aim / held_s))
        elif held_s < aim / 2:
            self.size = min(self.high, self.size * 2)


@dataclass
class _Run:
    """What one job run did, for its result."""

    job: str
    branch: str
    pace: _Pace
    batches: int = 0
    rows: int = 0
    promoted: bool = False

    def done(self, outcome: str, **extra: Any) -> dict[str, Any]:
        BRANCH_JOB_RUNS.add(1, {"job": self.job, "outcome": outcome})
        return {
            "branch": self.branch,
            "status": outcome,
            "batches": self.batches,
            "rows": self.rows,
            **extra,
        }


@dataclass(frozen=True, slots=True)
class _Step:
    """One batch transaction: rows it read, or the job's result when it was the last; how
    long it waited for the branch lock and whether a commit waited for it."""

    rows: int = 0
    finished: dict[str, Any] | None = None
    again: bool = False
    waited_s: float = 0.0
    contended: bool = False


def _payload_branch(job: JobRow) -> tuple[str, str]:
    if job.domain_id is None:
        raise ValueError(f"a {job.type} job needs a Domain")
    branch = job.payload.get("branch")
    if not isinstance(branch, str) or not branch:
        raise ValueError(f"a {job.type} job needs a `branch`")
    return job.domain_id, branch


class _Slot(StrEnum):
    ACQUIRED = "acquired"
    BUSY = "busy"
    """Every one of `job_concurrency` slots is taken."""
    RUNNING = "running"
    """Another job works on the same branch."""


_TRY_LOCK = text("SELECT pg_try_advisory_lock(:key)")
_UNLOCK_ALL = text("SELECT pg_advisory_unlock_all()")
_WAITERS = text(
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
    "AND classid = CAST((CAST(:key AS bigint) >> 32) & 4294967295 AS oid) "
    "AND objid = CAST(CAST(:key AS bigint) & 4294967295 AS oid) AND objsubid = 1"
)


@asynccontextmanager
async def _slot(
    engine: AsyncEngine, domain_id: str, branch: str, settings: MaterializeSettings
) -> AsyncIterator[_Slot]:
    """Hold, on a connection of its own, the branch's job lock and one of the
    `job_concurrency` slots shared by every worker (session advisory locks: a worker that
    dies releases them with its connection)."""
    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        try:
            branch_key = advisory_lock_key(domain_id, f"\x00branch-job\x00{branch}")
            if not (await auto.execute(_TRY_LOCK, {"key": branch_key})).scalar_one():
                yield _Slot.RUNNING
                return
            for slot in range(settings.job_concurrency):
                key = advisory_lock_key("", f"\x00branch-job-slot\x00{slot}")
                if (await auto.execute(_TRY_LOCK, {"key": key})).scalar_one():
                    yield _Slot.ACQUIRED
                    return
            yield _Slot.BUSY
        finally:
            await auto.execute(_UNLOCK_ALL)


async def _again(
    engine: AsyncEngine,
    job: JobRow,
    settings: MaterializeSettings,
    clock: Clock,
    *,
    delay_s: float | None = None,
) -> int:
    """Queue the job once more after `job_retry_seconds` (or `delay_s`); the running one then
    succeeds (or fails)."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    delay = settings.job_retry_seconds if delay_s is None else delay_s
    async with sessions() as session, session.begin():
        return await enqueue_job(
            session,
            job.type,
            domain_id=job.domain_id,
            payload=job.payload,
            priority=job.priority,
            run_after=clock() + timedelta(seconds=delay),
        )


async def _lock_branch(
    session: AsyncSession, refs: RefRepository, branch: str, runtime: StoreRuntime
) -> float:
    """Take the branch lock; returns how long it took."""
    started = time.perf_counter()
    await refs.lock(branch, timeout_ms=runtime.settings.commit.lock_timeout_ms)
    return time.perf_counter() - started


async def _waiters(session: AsyncSession, domain_id: str, branch: str) -> int:
    """Transactions waiting for the branch lock this transaction holds (commits)."""
    key = advisory_lock_key(domain_id, branch)
    return int((await session.execute(_WAITERS, {"key": key})).scalar_one())


async def _batches(
    engine: AsyncEngine,
    job: JobRow,
    run: _Run,
    step: Callable[[AsyncSession], Any],
    settings: MaterializeSettings,
    clock: Clock,
) -> dict[str, Any]:
    """Run `step` in one transaction after another until it finishes the job, sizing each
    batch by the time the previous one held the branch lock (`_Pace`)."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    failures = 0
    while True:
        started = time.perf_counter()
        try:
            async with sessions() as session, session.begin():
                outcome: _Step = await step(session)
        except LockTimeoutError:
            failures += 1
            run.pace.next(float("inf"), contended=True)
            if failures >= _ATTEMPTS:
                queued = await _again(engine, job, settings, clock)
                return run.done("deferred", detail="the branch stayed locked", again=queued)
            await asyncio.sleep(min(settings.job_retry_seconds, 1.0))
            continue
        except DBAPIError as error:
            failures += 1
            if sqlstate(error) not in _RETRYABLE or failures >= _ATTEMPTS:
                raise
            log.info("%s job on branch %s: batch retried after %s", run.job, run.branch, error)
            continue
        elapsed = time.perf_counter() - started
        BRANCH_JOB_BATCH_DURATION.record(elapsed, {"job": run.job})
        failures = 0
        if outcome.rows:
            BRANCH_JOB_BATCH_ROWS.record(run.pace.size, {"job": run.job})
            run.batches += 1
            run.rows += outcome.rows
            BRANCH_JOB_ROWS.add(outcome.rows, {"job": run.job})
            run.pace.next(max(elapsed - outcome.waited_s, 0.0), contended=outcome.contended)
        if outcome.finished is not None:
            if outcome.again:
                outcome.finished["again"] = await _again(engine, job, settings, clock)
            return outcome.finished


async def run_materialize_job(
    engine: AsyncEngine,
    job: JobRow,
    *,
    runtime: StoreRuntime | None = None,
    clock: Clock | None = None,
) -> Mapping[str, Any]:
    """Job handler of `store.materialize`: make the branch named by the payload a full copy
    in batches, from its base (an overlay) or from its tree (a `building` branch), then
    reconcile schema changes made meanwhile (module doc). Resumes where a previous run
    stopped.

    The result has `promoted` (whether this run made the branch a full copy) and `status`:
    `done`, `deferred` (no free slot; queued again), `skipped` (another job copies it) or
    `cancelled` (the branch is gone or being deleted).
    """
    runtime = runtime or default_runtime()
    now = clock or _now
    domain_id, branch = _payload_branch(job)
    settings = runtime.settings.materialize
    run = _Run("materialize", branch, _Pace.of(settings))
    async with _slot(engine, domain_id, branch, settings) as slot:
        if slot is _Slot.RUNNING:
            return run.done("skipped", promoted=False, detail="another job copies the branch")
        if slot is _Slot.BUSY:
            queued = await _again(engine, job, settings, now)
            return run.done("deferred", promoted=False, detail="no free slot", again=queued)

        async def step(session: AsyncSession) -> _Step:
            return await _materialize_step(session, domain_id, branch, run, runtime, now)

        try:
            return await _batches(engine, job, run, step, settings, now)
        except Exception as error:
            BRANCH_JOB_RUNS.add(1, {"job": run.job, "outcome": "failed"})
            final = job.attempts >= runtime.settings.jobs.max_attempts
            await _record_failure(engine, job, error, final=final, runtime=runtime, clock=now)
            raise


async def _record_failure(
    engine: AsyncEngine,
    job: JobRow,
    error: Exception,
    *,
    final: bool,
    runtime: StoreRuntime,
    clock: Clock,
) -> None:
    """Keep the error on the progress row. Once the job has no attempt left: queue the copy
    again with backoff while it has failed fewer than `copy_max_failures` times; then detach
    an overlay and build it from its tree; a build is marked `failed`."""
    domain_id, branch = _payload_branch(job)
    settings = runtime.settings.materialize
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    message = f"{type(error).__name__}: {error}"[:2000]
    try:
        async with sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            states = BranchMaterializationRepository(session, domain_id)
            state = await states.get(branch, for_update=True)
            if state is None:
                return
            if not final:
                await states.update(
                    branch, at=clock(), status=MaterializationStatus.PENDING, error=message
                )
                return
            failures = state.failures + 1
            fallback = failures >= settings.copy_max_failures
            if not fallback:
                delay = min(
                    settings.job_retry_seconds * 2**failures, settings.copy_retry_max_seconds
                )
                await states.update(
                    branch,
                    at=clock(),
                    status=MaterializationStatus.PENDING,
                    error=message,
                    failures=failures,
                )
                await enqueue_job(
                    session,
                    MATERIALIZE_JOB,
                    domain_id=domain_id,
                    payload=job.payload,
                    priority=job.priority,
                    run_after=clock() + timedelta(seconds=delay),
                )
                BRANCH_JOB_RUNS.add(1, {"job": "materialize", "outcome": "retried"})
                log.warning(
                    "copy of branch %s of Domain %s failed (%d of %d): retried in %.0f s",
                    branch,
                    domain_id,
                    failures,
                    settings.copy_max_failures,
                    delay,
                )
                return
            # Only a copy from the base can fall back to a build; a build, or the
            # reconciliation of a branch already full, has nothing else to try.
            detach = (
                state.strategy is MaterializationStrategy.BASE
                and state.phase not in _NEXT_RECONCILE
            )
            status = MaterializationStatus.PENDING if detach else MaterializationStatus.FAILED
            await states.update(branch, at=clock(), status=status, error=message, failures=failures)
        if detach:
            await _fall_back(engine, job, failures=failures, runtime=runtime, clock=clock)
            return
        BRANCH_JOB_RUNS.add(1, {"job": "materialize", "outcome": "given_up"})
        log.error(
            "build of branch %s of Domain %s failed %d times; queue a store.materialize job "
            "for it again once the cause is fixed: %s",
            branch,
            domain_id,
            failures,
            message,
        )
    except Exception:
        log.exception("could not record the failure of the copy of branch %s", branch)


async def _fall_back(
    engine: AsyncEngine, job: JobRow, *, failures: int, runtime: StoreRuntime, clock: Clock
) -> None:
    """Detach the overlay whose copy kept failing (`detach_to_build`); when its lock stays
    taken, queue the copy again so that its next failure tries once more."""
    domain_id, branch = _payload_branch(job)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            detached = await detach_to_build(
                session,
                domain_id,
                branch,
                runtime=runtime,
                at=clock(),
                failures=failures,
                unique=False,
            )
    except LockTimeoutError:
        await _again(engine, job, runtime.settings.materialize, clock)
        return
    if not detached:
        # No longer an overlay (made full or deleted meanwhile): nothing left to fall back from.
        async with sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            states = BranchMaterializationRepository(session, domain_id)
            state = await states.get(branch, for_update=True)
            if state is not None and state.active:
                await states.update(branch, at=clock(), status=MaterializationStatus.FAILED)
        BRANCH_JOB_RUNS.add(1, {"job": "materialize", "outcome": "given_up"})
        return
    BRANCH_JOB_RUNS.add(1, {"job": "materialize", "outcome": "fallback"})
    log.error(
        "copy of overlay branch %s of Domain %s failed %d times: detached from its base and "
        "rebuilt from its tree",
        branch,
        domain_id,
        failures,
    )


async def _materialize_step(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    run: _Run,
    runtime: StoreRuntime,
    clock: Clock,
) -> _Step:
    await scope_to_domain(session, domain_id)
    refs = RefRepository(session, domain_id)
    # The branch lock: no commit on the branch, and no copy on write into it from its base,
    # runs during the batch (their inserts of the same keys could deadlock with it). Then the
    # fence, before reading the ref: a promotion, re-fork or rebuild in progress ends first,
    # and this batch sees what it left.
    waited = await _lock_branch(session, refs, branch, runtime)
    await copy_fence(session, domain_id, branch, shared=True)
    # KEY SHARE: the ref (and the rows that reference it) cannot be deleted meanwhile.
    ref = await refs.get_deleting(branch, key_share=True)
    if ref is None or ref.deleted_at is not None:
        return _Step(
            finished=run.done("cancelled", promoted=False, detail="the branch no longer exists")
        )
    states = BranchMaterializationRepository(session, domain_id)
    state = await states.get(branch, for_update=True)
    if ref.materialization is Materialization.BUILDING:
        outcome = await _build_step(session, domain_id, ref, state, run, runtime, clock)
    elif ref.materialization is Materialization.OVERLAY and ref.base_branch is not None:
        outcome = await _copy_step(session, domain_id, ref, state, run, runtime, clock)
    elif state is not None and state.active and state.phase in _NEXT_RECONCILE:
        outcome = await _reconcile_step(session, domain_id, ref, state, run, runtime, clock)
    else:
        if state is not None and state.status is not MaterializationStatus.DONE:
            await states.finish(branch, at=clock())
        outcome = _Step(finished=run.done("done", promoted=run.promoted))
    if outcome.finished is not None:
        return outcome
    waiters = await _waiters(session, domain_id, branch)
    return replace(outcome, waited_s=waited, contended=run.pace.contended(waited, waiters))


async def _copy_step(
    session: AsyncSession,
    domain_id: str,
    ref: RefRow,
    state: MaterializationState | None,
    run: _Run,
    runtime: StoreRuntime,
    clock: Clock,
) -> _Step:
    """One batch of the copy of an overlay from its base (strategy `base`)."""
    branch = ref.name
    assert ref.base_branch is not None
    assert ref.base_generation is not None
    states = BranchMaterializationRepository(session, domain_id)
    now = clock()
    if (
        state is None
        or state.strategy is not MaterializationStrategy.BASE
        or (state.base_branch, state.base_generation) != (ref.base_branch, ref.base_generation)
    ):
        # First run, or re-forked since: start from the first row of the new fork point.
        state = await states.start(
            branch,
            base_branch=ref.base_branch,
            base_generation=ref.base_generation,
            status=MaterializationStatus.RUNNING,
            failures=0 if state is None else state.failures,
            reconcile=None if state is None else state.reconcile,
            at=now,
        )
    if state.rows_total is None:
        total = await _base_rows(session, domain_id, ref.base_branch)
        await states.update(branch, at=now, rows_total=total)
    if state.status is not MaterializationStatus.RUNNING:
        await states.update(branch, at=now, status=MaterializationStatus.RUNNING)
    if state.phase is MaterializationPhase.SWITCH:
        # The total was an estimate (`_base_rows`); every row has been read now.
        await states.update(branch, at=now, rows_total=state.rows_done)
        await _switch(session, domain_id, branch, runtime)
        run.promoted = True
        log.info("branch %s of Domain %s is now a full copy", branch, domain_id)
        if state.reconcile is not None:
            # Its schema changed while it was copied: recompute in batches what the change
            # recomputes on a full branch.
            await states.update(branch, at=clock(), phase=MaterializationPhase.PRUNE, cursor=None)
            return _Step()
        await states.finish(branch, at=clock())
        return _Step(finished=run.done("done", promoted=True))
    copied = next((c for c in _COPIED if c.phase is state.phase), None)
    if copied is None:
        # A phase of another strategy: start over from the base.
        await states.update(branch, at=now, phase=MaterializationPhase.RECORDS, cursor=None)
        return _Step()
    read, cursor = await _copy_batch(
        session,
        copied,
        domain_id=domain_id,
        branch=branch,
        base=ref.base_branch,
        generation=ref.base_generation,
        after=_decode(copied, state.cursor),
        limit=run.pace.size,
    )
    if cursor is None:
        await states.update(
            branch, at=now, phase=_NEXT[state.phase], cursor=None, rows_done=state.rows_done + read
        )
    else:
        await states.update(
            branch, at=now, cursor=_encode(cursor), rows_done=state.rows_done + read
        )
    return _Step(rows=read)


async def _tree_entries(
    session: AsyncSession, domain_id: str, head: RevisionRow, runtime: StoreRuntime
) -> int:
    """Records and Links in the tree of `head`."""
    settings = runtime.settings
    nodes = NodeRepository(
        session, domain_id, writes=settings.writes, batch=settings.nodes.prefetch_batch
    )
    tree = await nodes.load_tree(head.root)
    return sum(ref.count for name, ref in tree.partitions.items() if name != PARTITION_SCHEMA)


async def _build_step(
    session: AsyncSession,
    domain_id: str,
    ref: RefRow,
    state: MaterializationState | None,
    run: _Run,
    runtime: StoreRuntime,
    clock: Clock,
) -> _Step:
    """One batch of the build of a `building` branch from the tree of its head (strategy
    `tree`)."""
    branch = ref.name
    states = BranchMaterializationRepository(session, domain_id)
    now = clock()
    head = await RevisionRepository(session, domain_id).require(ref.head)
    if state is None or state.strategy is not MaterializationStrategy.TREE:
        state = await states.start(
            branch,
            base_branch=branch if state is None else state.base_branch,
            base_generation=head.generation,
            status=MaterializationStatus.RUNNING,
            strategy=MaterializationStrategy.TREE,
            failures=0 if state is None else state.failures,
            at=now,
        )
    if state.rows_total is None:
        await states.update(
            branch, at=now, rows_total=await _tree_entries(session, domain_id, head, runtime)
        )
    if state.status is not MaterializationStatus.RUNNING:
        await states.update(branch, at=now, status=MaterializationStatus.RUNNING)
    limit = run.pace.size
    phase = state.phase
    if phase is MaterializationPhase.CLEAR:
        batch = await build.clear_batch(session, domain_id, branch, limit=limit)
    elif phase is MaterializationPhase.TREE_HISTORY:
        batch = await build.history_batch(
            session, domain_id, branch, head, state.cursor, limit=limit, runtime=runtime
        )
    elif phase is MaterializationPhase.TREE_ROWS:
        batch = await build.rows_batch(
            session, domain_id, branch, head, state.cursor, limit=limit, runtime=runtime
        )
    elif phase is MaterializationPhase.DERIVED:
        batch = await build.derived_batch(
            session,
            domain_id,
            branch,
            head,
            state.cursor,
            limit=limit,
            runtime=runtime,
            expression_limits=runtime.schema.expression_limits,
        )
    elif phase is MaterializationPhase.PATHS:
        batch = await build.paths_batch(
            session, domain_id, branch, state.cursor, limit=limit, runtime=runtime
        )
    elif phase is MaterializationPhase.SWITCH:
        await _finish_build(session, domain_id, branch, runtime)
        await states.finish(branch, at=clock())
        run.promoted = True
        log.info("branch %s of Domain %s is built from its tree", branch, domain_id)
        return _Step(finished=run.done("done", promoted=True))
    else:
        # A phase of another strategy: start the build over.
        await states.update(branch, at=now, phase=MaterializationPhase.CLEAR, cursor=None)
        return _Step()
    done = state.rows_done + (batch.rows if phase is MaterializationPhase.TREE_ROWS else 0)
    if batch.cursor is None:
        await states.update(branch, at=now, phase=_NEXT_BUILD[phase], cursor=None, rows_done=done)
    else:
        await states.update(branch, at=now, cursor=batch.cursor, rows_done=done)
    return _Step(rows=max(batch.rows, 1))


async def _finish_build(
    session: AsyncSession, domain_id: str, branch: str, runtime: StoreRuntime
) -> None:
    """Make the built branch `full` and queue its per-Attribute indexes."""
    await RefRepository(session, domain_id).set_materialization(branch, Materialization.FULL)
    await _sync_indexes(session, domain_id, branch, runtime)


async def _reconcile_step(
    session: AsyncSession,
    domain_id: str,
    ref: RefRow,
    state: MaterializationState,
    run: _Run,
    runtime: StoreRuntime,
    clock: Clock,
) -> _Step:
    """One batch of the reconciliation of schema changes made while the branch was copied."""
    branch = ref.name
    states = BranchMaterializationRepository(session, domain_id)
    now = clock()
    limit = run.pace.size
    phase = state.phase
    if phase is MaterializationPhase.PRUNE:
        batch = await build.prune_batch(
            session,
            domain_id,
            branch,
            state.cursor,
            limit=limit,
            runtime=runtime,
            expression_limits=runtime.schema.expression_limits,
        )
    elif phase is MaterializationPhase.DERIVED:
        head = await RevisionRepository(session, domain_id).require(ref.head)
        batch = await build.derived_batch(
            session,
            domain_id,
            branch,
            head,
            state.cursor,
            limit=limit,
            runtime=runtime,
            expression_limits=runtime.schema.expression_limits,
        )
    elif phase is MaterializationPhase.PATHS:
        batch = await build.paths_batch(
            session, domain_id, branch, state.cursor, limit=limit, runtime=runtime
        )
    else:
        kinds = (state.reconcile or {}).get("refresh_kinds", [])
        batch = await build.search_batch(
            session, domain_id, branch, kinds, state.cursor, limit=limit, runtime=runtime
        )
    if batch.cursor is not None:
        await states.update(branch, at=now, cursor=batch.cursor)
        return _Step(rows=max(batch.rows, 1))
    following = _NEXT_RECONCILE[phase]
    if following is MaterializationPhase.DONE:
        await states.finish(branch, at=now)
        log.info("branch %s of Domain %s reconciled with its schema", branch, domain_id)
        return _Step(finished=run.done("done", promoted=run.promoted, reconciled=True))
    await states.update(branch, at=now, phase=following, cursor=None)
    return _Step(rows=max(batch.rows, 1))


async def _base_rows(session: AsyncSession, domain_id: str, base: str) -> int:
    """Rows a copy reads (the base branch's Records, Links and history entries), as the
    planner estimates them from the table statistics: counting them reads every row of the
    base, which at 10M Records takes longer than the statement timeout. The total feeds the
    progress report only, and is set to the rows read when the copy switches. Rows written
    after the fork point, which the copy skips, are few."""
    total = 0
    for copied in _COPIED:
        # Literals rather than parameters: the estimate is of a plan for these values.
        plan = await session.execute(
            text(
                f"EXPLAIN (FORMAT JSON) SELECT 1 FROM {copied.table.name} "
                f"WHERE domain_id = {sql_literal(domain_id)} AND branch = {sql_literal(base)}"
            )
        )
        found = plan.scalar_one()
        found = json.loads(found) if isinstance(found, str) else found
        total += int(found[0]["Plan"]["Plan Rows"])
    return total


def _quoted(column: str) -> str:
    return f'"{column}"'


def _tuple(prefix: str, size: int) -> str:
    return "(" + ", ".join(f":{prefix}{i}" for i in range(size)) + ")"


def _bind(prefix: str, values: Sequence[Any]) -> dict[str, Any]:
    return {f"{prefix}{i}": value for i, value in enumerate(values)}


def _encode(key: Sequence[Any]) -> list[Any]:
    return [value.hex() if isinstance(value, bytes) else value for value in key]


def _decode(copied: _Copied, cursor: Sequence[Any] | None) -> tuple[Any, ...] | None:
    if not cursor:
        return None
    values = list(cursor)
    if copied.table is MAT_HISTORY:
        values[-1] = bytes.fromhex(values[-1])
    return tuple(values)


async def _copy_batch(
    session: AsyncSession,
    copied: _Copied,
    *,
    domain_id: str,
    branch: str,
    base: str,
    generation: int,
    after: tuple[Any, ...] | None,
    limit: int,
) -> tuple[int, tuple[Any, ...] | None]:
    """Copy into the overlay the next `limit` base rows after `after`, in key order, that it
    reads, keeping the rows it holds. One statement: the batch is read along the primary key
    (`ORDER BY ... LIMIT`), inserted, and its last key returned. Returns the base rows read
    and the last key, `None` when none is left."""
    table = copied.table.name
    key = ", ".join(f"s.{c}" for c in copied.key)
    params: dict[str, Any] = {
        "domain": domain_id,
        "base": base,
        "branch": branch,
        "generation": generation,
        "limit": limit,
    }
    start = ""
    if after is not None:
        start = f" AND ({key}) > {_tuple('a', len(after))}"
        params.update(_bind("a", after))
    if copied.table is MAT_HISTORY:
        # OFFSET 0: a primary key probe of revisions per row, not a join the planner may
        # turn into a scan of every Revision of the Domain per row.
        visible = (
            " AND EXISTS (SELECT 1 FROM revisions r WHERE r.domain_id = :domain "
            "AND r.id = s.revision_id AND r.generation <= :generation OFFSET 0)"
        )
    else:
        visible = " AND s.revision_generation <= :generation"
    columns = [c.name for c in copied.table.columns]
    selected = ", ".join(
        f"CAST(:branch AS text) AS {_quoted(c)}"
        if c == "branch"
        else f"false AS {_quoted(c)}"
        if c == "deleted"
        else f"s.{_quoted(c)}"
        for c in columns
    )
    names = ", ".join(_quoted(c) for c in columns)
    last = ", ".join(copied.key)
    descending = ", ".join(f"{c} DESC" for c in copied.key)
    found = (
        await session.execute(
            text(
                f"WITH batch AS MATERIALIZED (SELECT {selected} FROM {table} s "
                f"WHERE s.domain_id = :domain AND s.branch = :base{start}{visible} "
                f"ORDER BY {key} LIMIT :limit), "
                f"copied AS (INSERT INTO {table} ({names}) SELECT {names} FROM batch "
                "ON CONFLICT DO NOTHING) "
                f"SELECT (SELECT count(*) FROM batch), {last} FROM batch "
                f"ORDER BY {descending} LIMIT 1"
            ),
            params,
        )
    ).one_or_none()
    if found is None:
        return 0, None
    read = int(found[0])
    return read, (tuple(found[1:]) if read >= limit else None)


async def _switch(
    session: AsyncSession, domain_id: str, branch: str, runtime: StoreRuntime
) -> None:
    """Make the overlay `branch`, whose base rows are all copied, a full copy. The caller
    holds the branch lock and read the ref after taking it."""
    refs = RefRepository(session, domain_id)
    await copy_fence(session, domain_id, branch)
    for table in (MAT_RECORDS, MAT_LINKS):
        await session.execute(
            text(
                f"DELETE FROM {table.name} WHERE domain_id = :domain AND branch = :branch "
                "AND deleted"
            ),
            {"domain": domain_id, "branch": branch},
        )
    await refs.set_materialization(branch, Materialization.FULL)
    await _sync_indexes(session, domain_id, branch, runtime)


async def _sync_indexes(
    session: AsyncSession, domain_id: str, branch: str, runtime: StoreRuntime
) -> None:
    """Queue the per-Attribute indexes of a branch that just became full (none is built in
    the caller's transaction)."""
    schema = await MaterializedRepository(
        session, domain_id, branch, writes=runtime.settings.writes
    ).schema()
    if schema is not None:
        # Imported here: the commit package imports the materializer.
        from lorebase_store.commit.schema import load_compiled

        compiled = load_compiled(schema.compiled)
        await BranchIndexes(
            session,
            domain_id,
            branch,
            settings=runtime.settings.indexes,
            constraints_only=constraints_only(runtime.settings),
            maintenance=runtime.index_maintenance,
            defer=True,
        ).sync(compiled)
        # Rows copied before the source rewrote them are stale.
        await queue_if_stale(session, domain_id, branch, compiled, runtime=runtime)


async def run_branch_delete_job(
    engine: AsyncEngine,
    job: JobRow,
    *,
    runtime: StoreRuntime | None = None,
    clock: Clock | None = None,
) -> Mapping[str, Any]:
    """Job handler of `store.branch_delete`: delete the rows of the hidden branch named by
    the payload in batches, then its ref (module doc).

    The result `status` is `done`, `waiting` (overlays still read the branch; queued again),
    `deferred` (no free slot; queued again), `skipped` (another job works on the branch) or
    `cancelled` (the branch is not being deleted).
    """
    runtime = runtime or default_runtime()
    now = clock or _now
    domain_id, branch = _payload_branch(job)
    settings = runtime.settings.materialize
    run = _Run("delete", branch, _Pace.of(settings))
    async with _slot(engine, domain_id, branch, settings) as slot:
        if slot is _Slot.RUNNING:
            return run.done("skipped", detail="another job works on the branch")
        if slot is _Slot.BUSY:
            queued = await _again(engine, job, settings, now)
            return run.done("deferred", detail="no free slot", again=queued)

        async def step(session: AsyncSession) -> _Step:
            return await _delete_step(session, domain_id, branch, run, settings, now)

        try:
            return await _batches(engine, job, run, step, settings, now)
        except Exception:
            BRANCH_JOB_RUNS.add(1, {"job": run.job, "outcome": "failed"})
            raise


async def _delete_step(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    run: _Run,
    settings: MaterializeSettings,
    clock: Clock,
) -> _Step:
    await scope_to_domain(session, domain_id)
    refs = RefRepository(session, domain_id)
    ref = await refs.get_deleting(branch)
    if ref is None:
        return _Step(finished=run.done("done"))
    if ref.deleted_at is None:
        return _Step(finished=run.done("cancelled", detail="the branch is not being deleted"))
    waiting = await refs.dependents(branch)
    if waiting:
        # Their reads go through this branch's rows until they are full copies.
        for overlay in waiting:
            if overlay.deleted_at is None and overlay.materialization is Materialization.OVERLAY:
                await queue_materialization(session, domain_id, overlay, at=clock())
        return _Step(finished=run.done("waiting", overlays=[r.name for r in waiting]), again=True)
    batch = await build.clear_batch(session, domain_id, branch, limit=run.pace.size)
    if batch.rows:
        return _Step(rows=batch.rows)
    await refs.delete(branch)
    log.info("deleted the rows and ref of branch %s of Domain %s", branch, domain_id)
    return _Step(finished=run.done("done"))
