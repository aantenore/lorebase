"""Lineage revisions of rows materialized before migration 0032: the backfill job and the
history fold it shares with rebuilds."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.tree import kind_partition
from lorebase_store.commit import commit, create_domain
from lorebase_store.jobs import LINEAGE_BACKFILL_JOB, run_jobs
from lorebase_store.materialize import create_branch, queue_lineage_sweep, run_lineage_backfill_job
from lorebase_store.materialize.rebuild import fold_history
from lorebase_store.materialize.rows import CREATED_KEY
from lorebase_store.repositories import MaterializedRepository
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import MaterializeSettings, StoreSettings

from .support import (
    AUTHOR,
    DOMAIN,
    ORIGIN,
    SUPPLIER,
    Clock,
    domain,
    part,
    schema_changes,
    supplier,
)

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
OVERLAY = "lineage-overlay"
ONE_A_BATCH = StoreRuntime.from_settings(
    StoreSettings(materialize=MaterializeSettings(job_batch_rows=1))
)
REVISIONS = text(
    "SELECT branch, kind_id, key, lineage_revisions FROM mat_records "
    "WHERE domain_id = :domain ORDER BY branch, kind_id, key"
)


@pytest.fixture
async def sessions(engine: AsyncEngine, runtime: StoreRuntime) -> Sessions:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=Clock())
    return factory


async def run(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock, changes: list[Any], **options: Any
) -> bytes:
    async with sessions() as session:
        result = await commit(
            session,
            DOMAIN,
            options.pop("branch", "main"),
            changes,
            author=AUTHOR,
            message="test",
            origin=ORIGIN,
            runtime=runtime,
            clock=clock,
            **options,
        )
    return result.revision_id


async def seed(sessions: Sessions, runtime: StoreRuntime) -> None:
    clock = Clock()
    await run(
        sessions,
        runtime,
        clock,
        [supplier("S-1", "Acme", country="IT"), supplier("S-2", "Bolt"), part("P-1", "Bearing")],
        schema_changes=schema_changes(),
    )
    await run(sessions, runtime, clock, [supplier("S-1", "Acme", country="FR")])
    async with sessions() as session:
        await create_branch(
            session,
            DOMAIN,
            OVERLAY,
            created_by=AUTHOR.principal,
            mode="overlay",
            runtime=runtime,
            clock=clock,
        )
    await run(sessions, runtime, clock, [supplier("S-2", "Bolt Works")], branch=OVERLAY)
    await run(sessions, runtime, clock, [supplier("S-3", "Cog")])


async def stored(sessions: Sessions) -> list[tuple[Any, ...]]:
    async with sessions() as session:
        return [tuple(row) for row in await session.execute(REVISIONS, {"domain": DOMAIN})]


async def drain(sessions: Sessions, engine: AsyncEngine, runtime: StoreRuntime) -> int:
    return await run_jobs(
        sessions,
        {LINEAGE_BACKFILL_JOB: lambda job: run_lineage_backfill_job(engine, job, runtime=runtime)},
    )


async def test_backfill_restores_what_commits_wrote(
    sessions: Sessions, engine: AsyncEngine, runtime: StoreRuntime
) -> None:
    await seed(sessions, runtime)
    expected = await stored(sessions)
    assert all(revs and CREATED_KEY in revs for *_, revs in expected)
    assert {branch for branch, *_ in expected} == {"main", OVERLAY}
    async with sessions() as session, session.begin():
        # Rows from before 0032, and a row updated since (no `$created`).
        await session.execute(text("UPDATE mat_records SET lineage_revisions = NULL"))
        await session.execute(
            text(
                "UPDATE mat_records SET lineage_revisions = lineage_revisions - '$created' "
                "WHERE key = 'S-1'"
            )
        )
        await session.execute(
            text("UPDATE mat_records SET lineage_revisions = '{\"att\": \"00\"}' WHERE key = 'S-1'")
        )

    sweep = await queue_lineage_sweep(sessions)
    assert sweep is not None
    assert await queue_lineage_sweep(sessions) is None  # queued: not again
    # The sweep, then one job per branch, one row per batch.
    ran = await run_jobs(
        sessions,
        {
            LINEAGE_BACKFILL_JOB: lambda job: run_lineage_backfill_job(
                engine, job, runtime=ONE_A_BATCH
            )
        },
    )
    assert ran == 3
    restored = await stored(sessions)
    # S-1 held a partial map: the fold replaces it with the full one.
    assert restored == expected
    assert await queue_lineage_sweep(sessions) is None  # done: never again

    # Run again: nothing left to write.
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO jobs (type, domain_id, payload) "
                "VALUES (:type, :domain, CAST(:payload AS jsonb))"
            ),
            {"type": LINEAGE_BACKFILL_JOB, "domain": DOMAIN, "payload": '{"branch": "main"}'},
        )
    assert await drain(sessions, engine, runtime) == 1
    async with sessions() as session:
        result = (
            await session.execute(
                text("SELECT result FROM jobs WHERE type = :type ORDER BY id DESC LIMIT 1"),
                {"type": LINEAGE_BACKFILL_JOB},
            )
        ).scalar_one()
    assert result["written"] == 0
    assert await stored(sessions) == expected


async def test_records_without_history_get_an_empty_map(
    sessions: Sessions, engine: AsyncEngine, runtime: StoreRuntime
) -> None:
    await seed(sessions, runtime)
    async with sessions() as session, session.begin():
        await session.execute(text("UPDATE mat_records SET lineage_revisions = NULL"))
        await session.execute(
            text("DELETE FROM mat_history WHERE element_key = 'S-3' AND branch = 'main'")
        )
    await queue_lineage_sweep(sessions)
    assert await drain(sessions, engine, runtime) == 3
    found = {(branch, key): revs for branch, _, key, revs in await stored(sessions)}
    assert found[("main", "S-3")] == {}
    assert CREATED_KEY in found[("main", "S-1")]


async def test_fold_names_a_creation_only_for_the_change_that_created(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    created = await run(
        sessions,
        runtime,
        clock,
        [supplier("S-1", "Acme", country="IT"), supplier("S-2", "Bolt")],
        schema_changes=schema_changes(),
    )
    updated = await run(sessions, runtime, clock, [supplier("S-1", "Acme", country="FR")])
    async with sessions() as session, session.begin():
        # History that starts after S-1 was created (its creation row is gone).
        await session.execute(
            text("DELETE FROM mat_history WHERE element_key = 'S-1' AND revision_id = :revision"),
            {"revision": created},
        )
        repository = MaterializedRepository(session, DOMAIN, "main", writes=runtime.settings.writes)
        folded = await fold_history(repository, kind_partition(SUPPLIER), batch=100)
    assert CREATED_KEY not in folded["S-1"].revisions
    assert set(folded["S-1"].revisions.values()) == {updated.hex()}
    assert folded["S-2"].revisions[CREATED_KEY] == created.hex()
