"""Large branches in the background (`lorebase_store.materialize.background`): a copy starts
as an overlay and switches to a full copy after its rows are copied in batches, commits during
the copy stay correct, a failed copy resumes, and a large deletion hides the branch at once
and deletes its rows in batches; also as a role subject to row-level security."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_store.commit import BranchMaterializingError, SchemaUpsert
from lorebase_store.db import create_engine
from lorebase_store.jobs import BRANCH_DELETE_JOB, INDEX_JOB, MATERIALIZE_JOB, JobRow
from lorebase_store.materialize import (
    BranchDeletingError,
    BranchDeletion,
    BranchMode,
    MaterializationMode,
    background,
    create_branch,
    delete_branch,
    promote,
    run_branch_delete_job,
    run_materialize_job,
    verify,
)
from lorebase_store.models.enums import Materialization
from lorebase_store.reads import head_schema
from lorebase_store.repositories import (
    BranchMaterializationRepository,
    MaterializationPhase,
    MaterializationState,
    MaterializationStatus,
    MaterializedRepository,
    RefRepository,
    scope_to_domain,
)
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, MaterializeSettings, StoreSettings

from .read_support import assert_lexicon_in_step, lexicon, part_with
from .support import DOMAIN, PART, SUPPLIER, VAT, supplied, supplier, wait_for_lock_waiter
from .test_overlay import (
    counted_supplier,
    drop,
    put,
    runtimes,
    seeded_sessions,
    snapshot,
    unlink,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def sessions(engine: AsyncEngine, runtime: StoreRuntime) -> Sessions:
    """`main` with the small graph of the overlay tests."""
    return await seeded_sessions(engine, runtime)


Sessions = async_sessionmaker[AsyncSession]

BIG = "big"
REFERENCE = "reference"


def runtime_with(**materialize: Any) -> StoreRuntime:
    return StoreRuntime.from_settings(StoreSettings(materialize=MaterializeSettings(**materialize)))


BACKGROUND = runtime_with(async_copy_min_rows=1, async_delete_min_rows=1, job_batch_rows=3)
"""Every copy and deletion of the small graph goes to the background, three rows a batch."""
INLINE = runtime_with(async_copy_min_rows=10**9, async_delete_min_rows=10**9)
"""Copies and deletions in the calling transaction, as before."""


def job(job_type: str, branch: str, *, attempts: int = 1) -> JobRow:
    now = datetime.now(UTC)
    return JobRow(
        id=0,
        type=job_type,
        domain_id=DOMAIN,
        payload={"branch": branch},
        priority=0,
        attempts=attempts,
        run_after=now,
        locked_by="test",
        created_at=now,
    )


async def create(sessions: Sessions, name: str, runtime: StoreRuntime, **options: Any) -> Any:
    async with sessions() as session:
        return await create_branch(
            session,
            DOMAIN,
            name,
            created_by="usr_test",
            mode=BranchMode.COPY,
            runtime=runtime,
            **options,
        )


async def state_of(sessions: Sessions, branch: str) -> MaterializationState | None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        return await BranchMaterializationRepository(session, DOMAIN).get(branch)


async def materialization(sessions: Sessions, branch: str) -> Materialization:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        return (await RefRepository(session, DOMAIN).require(branch)).materialization


async def step(sessions: Sessions, branch: str, runtime: StoreRuntime = BACKGROUND) -> bool:
    """One batch of the background copy of `branch`; whether it finished."""
    run = background._Run("materialize", branch)
    async with sessions() as session, session.begin():
        outcome = await background._materialize_step(
            session, DOMAIN, branch, run, runtime, lambda: datetime.now(UTC)
        )
    return outcome.finished is not None


async def rows_of(sessions: Sessions, branch: str) -> dict[str, int]:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        counts = {}
        for table in ("mat_records", "mat_links", "mat_history", "mat_schema"):
            found = await session.execute(
                text(f"SELECT count(*) FROM {table} WHERE domain_id = :d AND branch = :b"),
                {"d": DOMAIN, "b": branch},
            )
            counts[table] = int(found.scalar_one())
        return counts


async def jobs_of(sessions: Sessions, job_type: str) -> list[tuple[dict[str, Any], str]]:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = await session.execute(
            text("SELECT payload, status::text FROM jobs WHERE type = :t ORDER BY id"),
            {"t": job_type},
        )
        return [(dict(payload), status) for payload, status in found.all()]


async def history(sessions: Sessions, branch: str, kind_id: str, key: str) -> list[Any]:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        rows = await MaterializedRepository(
            session, DOMAIN, branch, writes=BACKGROUND.settings.writes
        ).history(f"k:{kind_id}", key)
    return [(h.old_hash, h.new_hash) for h in rows]


async def assert_reads_like(sessions: Sessions, branch: str, reference: str) -> None:
    """`branch` reads like `reference` through every read primitive, history included, and
    both verify against their trees."""
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = await head_schema(session, DOMAIN, branch, runtime=BACKGROUND)
        assert found is not None
        for reader in runtimes():
            assert await snapshot(session, branch, found.schema, reader) == await snapshot(
                session, reference, found.schema, reader
            )
        # The word lexicon follows the batches and the switch.
        for name in (branch, reference):
            await assert_lexicon_in_step(session, name)
    for kind_id, key in ((SUPPLIER, "S-1"), (SUPPLIER, "S-2"), (PART, "P-1"), (PART, "P-4")):
        assert await history(sessions, branch, kind_id, key) == await history(
            sessions, reference, kind_id, key
        )
    for name in (branch, reference):
        async with sessions() as session:
            report = await verify(session, DOMAIN, name, runtime=BACKGROUND, deep=True)
        assert report.ok, (name, report.divergences)


async def test_a_large_copy_starts_as_an_overlay_and_switches_once_copied(
    sessions: Sessions, engine: AsyncEngine
) -> None:
    created = await create(sessions, BIG, BACKGROUND)
    assert created.mode is MaterializationMode.COPY
    assert created.pending
    assert await materialization(sessions, BIG) is Materialization.OVERLAY
    state = await state_of(sessions, BIG)
    assert state is not None
    assert (state.status, state.phase, state.rows_done) == (
        MaterializationStatus.PENDING,
        MaterializationPhase.RECORDS,
        0,
    )
    assert await jobs_of(sessions, MATERIALIZE_JOB) == [({"branch": BIG}, "queued")]
    reference = await create(sessions, REFERENCE, INLINE)
    assert not reference.pending
    assert await materialization(sessions, REFERENCE) is Materialization.FULL
    await assert_reads_like(sessions, BIG, REFERENCE)

    # Batch by batch, the branch reads the same while its own rows fill up.
    phases = []
    while not await step(sessions, BIG):
        state = await state_of(sessions, BIG)
        assert state is not None
        assert state.status is MaterializationStatus.RUNNING
        phases.append(state.phase)
        assert await materialization(sessions, BIG) is Materialization.OVERLAY
        await assert_reads_like(sessions, BIG, REFERENCE)
    assert phases[-1] is MaterializationPhase.SWITCH
    assert {MaterializationPhase.LINKS, MaterializationPhase.HISTORY} <= set(phases)
    assert await materialization(sessions, BIG) is Materialization.FULL
    state = await state_of(sessions, BIG)
    assert state is not None
    assert state.status is MaterializationStatus.DONE
    assert state.finished_at is not None
    assert state.rows_total is not None
    assert state.rows_done == state.rows_total
    assert await rows_of(sessions, BIG) == await rows_of(sessions, REFERENCE)
    await assert_reads_like(sessions, BIG, REFERENCE)
    # A full branch: the job has nothing left to do.
    result = await run_materialize_job(engine, job(MATERIALIZE_JOB, BIG), runtime=BACKGROUND)
    assert (result["status"], result["promoted"]) == ("done", False)


async def test_the_job_copies_every_batch_and_queues_the_indexes_of_a_full_branch(
    sessions: Sessions, engine: AsyncEngine
) -> None:
    await create(sessions, BIG, BACKGROUND)
    await create(sessions, REFERENCE, INLINE)
    result = await run_materialize_job(engine, job(MATERIALIZE_JOB, BIG), runtime=BACKGROUND)
    assert (result["status"], result["promoted"]) == ("done", True)
    assert result["batches"] > 3
    assert await materialization(sessions, BIG) is Materialization.FULL
    await assert_reads_like(sessions, BIG, REFERENCE)
    builds = [p for p, _ in await jobs_of(sessions, INDEX_JOB) if p["branch"] == BIG]
    assert builds, "the per-Attribute indexes of the full copy are queued, not built inline"


async def test_commits_during_the_copy_stay_correct(sessions: Sessions) -> None:
    await create(sessions, BIG, BACKGROUND)
    await create(sessions, REFERENCE, INLINE)
    # A tight row limit would promote an overlay at its next commit; not while it is copied.
    tight = runtime_with(
        async_copy_min_rows=1, async_delete_min_rows=1, job_batch_rows=3, overlay_max_rows=1
    )
    for _ in range(2):
        assert not await step(sessions, BIG)
    # The branch changes rows already copied and rows not copied yet.
    await put(
        sessions,
        tight,
        [BIG, REFERENCE],
        [
            supplier("S-1", "Alpha Bearings Branch", vat="IT1", rating="6"),
            part_with("P-5", "Spring washer", main_supplier="S-3"),
            supplied("P-5", "S-3"),
            unlink("P-3", "S-1"),
            drop(PART, "P-3"),
        ],
    )
    assert await materialization(sessions, BIG) is Materialization.OVERLAY
    await assert_reads_like(sessions, BIG, REFERENCE)
    # The base moves: copy on write keeps the fork state for the copy still running.
    await put(
        sessions,
        tight,
        ["main"],
        [
            supplier("S-2", "Beta Seals Main", vat="IT2", rating="1"),
            part_with("P-1", "Tapered roller bearing XL", main_supplier="S-1"),
            unlink("P-4", "S-2"),
            drop(PART, "P-4"),
        ],
    )
    await assert_reads_like(sessions, BIG, REFERENCE)
    for _ in range(3):
        assert not await step(sessions, BIG)
    await put(
        sessions,
        tight,
        [BIG, REFERENCE],
        [part_with("P-3", "Ball bearing, sealed"), supplied("P-3", "S-2")],
    )
    await assert_reads_like(sessions, BIG, REFERENCE)
    # A schema change needs the full copy: refused until the copy is done.
    kind = counted_supplier()
    attributes = dict(kind.attributes)
    attributes[VAT] = attributes[VAT].model_copy(update={"description": "Tax number."})
    changed = SchemaUpsert(kind.model_copy(update={"attributes": attributes}))
    with pytest.raises(BranchMaterializingError):
        await put(sessions, tight, [BIG], [], schema_changes=[changed])
    while not await step(sessions, BIG):
        pass
    assert await materialization(sessions, BIG) is Materialization.FULL
    await assert_reads_like(sessions, BIG, REFERENCE)
    await put(sessions, tight, [BIG, REFERENCE], [], schema_changes=[changed])
    await put(sessions, tight, ["main"], [supplier("S-3", "Gamma Main", vat="IT3")])
    await assert_reads_like(sessions, BIG, REFERENCE)


async def test_a_promotion_during_the_copy_waits_for_the_batch_and_ends_it(
    sessions: Sessions, engine: AsyncEngine
) -> None:
    """A batch inserts with ON CONFLICT DO NOTHING: run while a promotion deletes tombstones,
    it could insert a deleted row again. The copy fence makes it wait, and the copy then finds
    a full branch."""
    await create(sessions, BIG, BACKGROUND)
    await create(sessions, REFERENCE, INLINE)
    assert not await step(sessions, BIG)
    await put(sessions, BACKGROUND, [BIG, REFERENCE], [unlink("P-3", "S-1"), drop(PART, "P-3")])
    async with sessions() as promoting, promoting.begin():
        await scope_to_domain(promoting, DOMAIN)
        assert await promote(promoting, DOMAIN, BIG, runtime=BACKGROUND)
        batch = asyncio.create_task(step(sessions, BIG))
        await wait_for_lock_waiter(engine, batch, locktype="advisory", what="copy fence")
    assert await batch, "the branch is full: the copy is done"
    assert await materialization(sessions, BIG) is Materialization.FULL
    state = await state_of(sessions, BIG)
    assert state is not None
    assert state.status is MaterializationStatus.DONE
    await assert_reads_like(sessions, BIG, REFERENCE)


async def test_a_failed_copy_keeps_its_progress_and_resumes(
    sessions: Sessions, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await create(sessions, BIG, BACKGROUND)
    await create(sessions, REFERENCE, INLINE)
    copy_batch = background._copy_batch
    calls = 0

    async def failing(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls > 2:
            raise RuntimeError("disk full")
        return await copy_batch(*args, **kwargs)

    monkeypatch.setattr(background, "_copy_batch", failing)
    with pytest.raises(RuntimeError, match="disk full"):
        await run_materialize_job(engine, job(MATERIALIZE_JOB, BIG), runtime=BACKGROUND)
    state = await state_of(sessions, BIG)
    assert state is not None
    assert state.status is MaterializationStatus.PENDING, "attempts are left: retried"
    assert state.error == "RuntimeError: disk full"
    assert state.rows_done == 6
    kept = state.cursor
    await assert_reads_like(sessions, BIG, REFERENCE)

    last = job(MATERIALIZE_JOB, BIG, attempts=BACKGROUND.settings.jobs.max_attempts)
    with pytest.raises(RuntimeError):
        await run_materialize_job(engine, last, runtime=BACKGROUND)
    state = await state_of(sessions, BIG)
    assert state is not None
    assert state.status is MaterializationStatus.FAILED
    assert state.cursor == kept
    assert await materialization(sessions, BIG) is Materialization.OVERLAY
    await assert_reads_like(sessions, BIG, REFERENCE)

    # Run again (queued by hand): it resumes from the cursor, and batches it repeats copy
    # nothing twice.
    monkeypatch.setattr(background, "_copy_batch", copy_batch)
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await BranchMaterializationRepository(session, DOMAIN).update(
            BIG, at=datetime.now(UTC), cursor=None, rows_done=0
        )
    result = await run_materialize_job(engine, job(MATERIALIZE_JOB, BIG), runtime=BACKGROUND)
    assert (result["status"], result["promoted"]) == ("done", True)
    state = await state_of(sessions, BIG)
    assert state is not None
    assert (state.status, state.error) == (MaterializationStatus.DONE, None)
    assert await rows_of(sessions, BIG) == await rows_of(sessions, REFERENCE)
    await assert_reads_like(sessions, BIG, REFERENCE)


async def test_jobs_wait_for_a_free_slot(sessions: Sessions, engine: AsyncEngine) -> None:
    await create(sessions, BIG, BACKGROUND)
    single = runtime_with(async_copy_min_rows=1, job_concurrency=1, job_retry_seconds=30)
    async with background._slot(engine, DOMAIN, "other", single.settings.materialize):
        result = await run_materialize_job(engine, job(MATERIALIZE_JOB, BIG), runtime=single)
        assert result["status"] == "deferred"
        async with background._slot(engine, DOMAIN, BIG, single.settings.materialize) as held:
            assert held is background._Slot.BUSY
    assert [s for _, s in await jobs_of(sessions, MATERIALIZE_JOB)] == ["queued", "queued"]
    assert await materialization(sessions, BIG) is Materialization.OVERLAY


async def test_a_large_deletion_hides_the_branch_and_deletes_its_rows_in_batches(
    sessions: Sessions, engine: AsyncEngine
) -> None:
    await create(sessions, "doomed", INLINE)
    async with sessions() as session:
        await create_branch(
            session,
            DOMAIN,
            "agent/reader",
            created_by="usr_test",
            source="doomed",
            mode=BranchMode.OVERLAY,
            runtime=BACKGROUND,
        )
    await put(sessions, BACKGROUND, ["agent/reader"], [supplier("S-2", "Beta", vat="IT2")])
    await create(sessions, REFERENCE, INLINE)
    await put(sessions, BACKGROUND, [REFERENCE], [supplier("S-2", "Beta", vat="IT2")])
    before = await rows_of(sessions, "doomed")

    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        assert (
            await delete_branch(session, DOMAIN, "doomed", runtime=BACKGROUND)
            is BranchDeletion.SCHEDULED
        )
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        refs = RefRepository(session, DOMAIN)
        assert await refs.get("doomed") is None
        assert "doomed" not in [r.name for r in await refs.all()]
        hidden = await refs.get_deleting("doomed")
        assert hidden is not None
        assert hidden.deleted_at is not None
    assert await rows_of(sessions, "doomed") == before, "rows stay until the job runs"
    with pytest.raises(BranchDeletingError):
        await create(sessions, "doomed", INLINE)
    # The overlay on it still reads its rows; its copy is queued.
    state = await state_of(sessions, "agent/reader")
    assert state is not None
    assert state.status is MaterializationStatus.PENDING
    await assert_reads_like(sessions, "agent/reader", REFERENCE)

    waiting = await run_branch_delete_job(
        engine, job(BRANCH_DELETE_JOB, "doomed"), runtime=BACKGROUND
    )
    assert (waiting["status"], waiting["overlays"]) == ("waiting", ["agent/reader"])
    assert await rows_of(sessions, "doomed") == before
    copied = await run_materialize_job(
        engine, job(MATERIALIZE_JOB, "agent/reader"), runtime=BACKGROUND
    )
    assert copied["promoted"]
    done = await run_branch_delete_job(engine, job(BRANCH_DELETE_JOB, "doomed"), runtime=BACKGROUND)
    assert done["status"] == "done"
    assert done["batches"] > 3
    assert done["rows"] == sum(v for k, v in before.items() if k != "mat_schema")
    assert await rows_of(sessions, "doomed") == {
        "mat_records": 0,
        "mat_links": 0,
        "mat_history": 0,
        "mat_schema": 0,
    }
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        assert await RefRepository(session, DOMAIN).get_deleting("doomed") is None
        assert await lexicon(session, "doomed") == {}
    await assert_reads_like(sessions, "agent/reader", REFERENCE)
    # The name is free again; a small branch is deleted at once.
    await create(sessions, "doomed", INLINE)
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        assert (
            await delete_branch(session, DOMAIN, "doomed", runtime=INLINE) is BranchDeletion.DELETED
        )
        assert await RefRepository(session, DOMAIN).get_deleting("doomed") is None


async def test_deleting_a_branch_during_its_copy_stops_the_copy(
    sessions: Sessions, engine: AsyncEngine
) -> None:
    await create(sessions, BIG, BACKGROUND)
    for _ in range(4):
        assert not await step(sessions, BIG)
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        assert (
            await delete_branch(session, DOMAIN, BIG, runtime=BACKGROUND)
            is BranchDeletion.SCHEDULED
        )
    assert [s for _, s in await jobs_of(sessions, MATERIALIZE_JOB)] == ["cancelled"]
    stopped = await run_materialize_job(engine, job(MATERIALIZE_JOB, BIG), runtime=BACKGROUND)
    assert stopped["status"] == "cancelled"
    done = await run_branch_delete_job(engine, job(BRANCH_DELETE_JOB, BIG), runtime=BACKGROUND)
    assert done["status"] == "done"
    assert await state_of(sessions, BIG) is None
    assert sum((await rows_of(sessions, BIG)).values()) == 0


@asynccontextmanager
async def rls_engine(engine: AsyncEngine, database_url: str) -> AsyncIterator[AsyncEngine]:
    """An engine whose connections act as a role that owns nothing and is subject to the
    row-level security policies, like the application role."""
    role = f"lorebase_app_{uuid.uuid4().hex[:8]}"
    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await auto.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
        await auto.exec_driver_sql(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}"
        )
        await auto.exec_driver_sql(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {role}")
    restricted = create_engine(DatabaseSettings(), url=database_url, pooled=False)

    @event.listens_for(restricted.sync_engine, "connect")
    def _as_role(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET ROLE {role}")
        cursor.close()

    try:
        yield restricted
    finally:
        await restricted.dispose()
        async with engine.connect() as connection:
            auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await auto.exec_driver_sql(f"DROP OWNED BY {role}")
            await auto.exec_driver_sql(f"DROP ROLE {role}")


async def test_background_jobs_run_as_a_role_subject_to_row_level_security(
    sessions: Sessions, engine: AsyncEngine, database_url: str
) -> None:
    async with rls_engine(engine, database_url) as restricted:
        as_app = async_sessionmaker(restricted, expire_on_commit=False)
        async with as_app() as session, session.begin():
            assert not (await session.execute(text("SELECT current_user = session_user"))).scalar()
            # Without a Domain scope, the policies hide every row.
            assert (await session.execute(text("SELECT count(*) FROM refs"))).scalar_one() == 0
        created = await create(as_app, BIG, BACKGROUND)
        assert created.pending
        await create(as_app, REFERENCE, INLINE)
        result = await run_materialize_job(
            restricted, job(MATERIALIZE_JOB, BIG), runtime=BACKGROUND
        )
        assert (result["status"], result["promoted"]) == ("done", True)
        await assert_reads_like(sessions, BIG, REFERENCE)
        async with as_app() as session, session.begin():
            await scope_to_domain(session, DOMAIN)
            assert (
                await delete_branch(session, DOMAIN, BIG, runtime=BACKGROUND)
                is BranchDeletion.SCHEDULED
            )
        done = await run_branch_delete_job(
            restricted, job(BRANCH_DELETE_JOB, BIG), runtime=BACKGROUND
        )
        assert done["status"] == "done"
    assert sum((await rows_of(sessions, BIG)).values()) == 0
