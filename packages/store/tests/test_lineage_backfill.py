"""Lineage revisions of rows materialized before migration 0032: the backfill job and the
history fold it shares with rebuilds."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import new_ulid
from lorebase_core.tree import kind_partition
from lorebase_store import jobs
from lorebase_store.commit import commit, create_domain
from lorebase_store.jobs import LINEAGE_BACKFILL_JOB, run_jobs
from lorebase_store.materialize import (
    create_branch,
    queue_lineage_sweep,
    rebuild,
    run_lineage_backfill_job,
)
from lorebase_store.materialize.lineage_backfill import backfill_payload
from lorebase_store.materialize.rebuild import fold_history
from lorebase_store.materialize.rows import CREATED_KEY
from lorebase_store.merging import MergeRequest, MergeService
from lorebase_store.models.enums import ProposalOrigin, ProposalStatus
from lorebase_store.objectstore import PgObjectStore
from lorebase_store.repositories import MaterializedRepository, ProposalRepository, ProposalRow
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import MaterializeSettings, StoreSettings

from .support import (
    AUTHOR,
    COUNTRY,
    DOMAIN,
    LEGAL_NAME,
    ORIGIN,
    SUPPLIER,
    SUPPLIER_CODE,
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
        objects = PgObjectStore(session, DOMAIN, writes=runtime.settings.writes)
        folded = await fold_history(
            repository, kind_partition(SUPPLIER), batch=100, objects=objects
        )
    assert CREATED_KEY not in folded["S-1"].revisions
    assert set(folded["S-1"].revisions.values()) == {updated.hex()}
    assert folded["S-2"].revisions[CREATED_KEY] == created.hex()


MAIN_ROWS = text(
    "SELECT kind_id, key, lineage, lineage_revisions, updated_revision, updated_at "
    "FROM mat_records WHERE domain_id = :domain AND branch = 'main' ORDER BY kind_id, key"
)


async def main_rows(sessions: Sessions) -> list[tuple[Any, ...]]:
    async with sessions() as session:
        return [tuple(row) for row in await session.execute(MAIN_ROWS, {"domain": DOMAIN})]


def revisions_by_key(rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    return {row[1]: row[3] for row in rows}


async def merge(sessions: Sessions, runtime: StoreRuntime, clock: Clock, source: str) -> bytes:
    """Merge `source` into main through a Proposal; returns the new head of main."""
    async with sessions() as session, session.begin():
        now = clock()
        proposal = ProposalRow(
            id=f"prp_{new_ulid()}",
            title=f"Merge {source}",
            description=None,
            source_ref=source,
            target_ref="main",
            origin=ProposalOrigin.MANUAL,
            status=ProposalStatus.OPEN,
            author=AUTHOR.principal,
            created_at=now,
            checks={},
            policies={},
            updated_at=now,
        )
        await ProposalRepository(session, DOMAIN).insert(proposal)
        result = await MergeService(session, DOMAIN, runtime=runtime, clock=clock).merge(
            MergeRequest(proposal=proposal, actor=AUTHOR.principal)
        )
        assert result.merged
        head = await session.execute(
            text("SELECT head FROM refs WHERE domain_id = :d AND name = 'main'"), {"d": DOMAIN}
        )
        return bytes(head.scalar_one())


async def fork_side(sessions: Sessions, runtime: StoreRuntime, clock: Clock) -> bytes:
    created = await run(
        sessions,
        runtime,
        clock,
        [supplier("S-1", "Acme", country="IT"), supplier("S-2", "Bolt")],
        schema_changes=schema_changes(),
    )
    async with sessions() as session:
        await create_branch(
            session, DOMAIN, "side", created_by=AUTHOR.principal, runtime=runtime, clock=clock
        )
    return created


async def test_a_fast_forward_names_the_revisions_a_rebuild_folds(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    created = await fork_side(sessions, runtime, clock)
    moved = await run(
        sessions, runtime, clock, [supplier("S-1", "Acme", country="FR")], branch="side"
    )
    renamed = await run(
        sessions, runtime, clock, [supplier("S-1", "Acme Ltd", country="FR")], branch="side"
    )
    last = await run(
        sessions, runtime, clock, [supplier("S-2", "Bolt", country="DE")], branch="side"
    )
    assert await merge(sessions, runtime, clock, "side") == last
    incremental = await main_rows(sessions)
    s1 = revisions_by_key(incremental)["S-1"]
    assert s1[COUNTRY] == moved.hex()
    assert s1[LEGAL_NAME] == renamed.hex()
    assert s1[SUPPLIER_CODE] == created.hex()
    async with sessions() as session, session.begin():
        await rebuild(session, DOMAIN, "main", runtime=runtime)
    assert await main_rows(sessions) == incremental


async def test_recompute_names_only_the_values_an_older_merge_changed(
    sessions: Sessions, engine: AsyncEngine, runtime: StoreRuntime
) -> None:
    clock = Clock()
    created = await fork_side(sessions, runtime, clock)
    await run(sessions, runtime, clock, [supplier("S-1", "Acme", country="FR")], branch="side")
    await run(sessions, runtime, clock, [supplier("S-2", "Bolt Works")])
    head = await merge(sessions, runtime, clock, "side")
    merged = head.hex()
    expected = revisions_by_key(await main_rows(sessions))
    assert expected["S-1"][COUNTRY] == merged
    assert expected["S-1"][SUPPLIER_CODE] == created.hex()

    # As a merge made before its changeset was restricted left it: the source's whole
    # Lineage in the history, and every value of the Record naming the merge.
    whole = {SUPPLIER_CODE: "prv_a", LEGAL_NAME: "prv_b", COUNTRY: "prv_c"}
    older = {**dict.fromkeys(whole, merged), CREATED_KEY: created.hex()}
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE mat_history SET provenance = CAST(:whole AS jsonb) "
                "WHERE domain_id = :d AND branch = 'main' AND element_key = 'S-1' "
                "AND revision_id = :merge"
            ),
            {"whole": json.dumps(whole), "d": DOMAIN, "merge": head},
        )
        await session.execute(
            text(
                "UPDATE mat_records SET lineage_revisions = CAST(:revs AS jsonb) "
                "WHERE domain_id = :d AND branch = 'main' AND key = 'S-1'"
            ),
            {"revs": json.dumps(older), "d": DOMAIN},
        )

    async def backfill(payload: dict[str, Any]) -> None:
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO jobs (type, domain_id, payload) "
                    "VALUES (:type, :domain, CAST(:payload AS jsonb))"
                ),
                {"type": LINEAGE_BACKFILL_JOB, "domain": DOMAIN, "payload": json.dumps(payload)},
            )
        assert await drain(sessions, engine, runtime) == 1

    # A plain backfill leaves complete rows alone; a recompute diffs the merge step.
    await backfill(backfill_payload("main"))
    assert revisions_by_key(await main_rows(sessions))["S-1"] == older
    await backfill(backfill_payload("main", recompute=True))
    current = revisions_by_key(await main_rows(sessions))
    assert current["S-1"] == {**older, SUPPLIER_CODE: created.hex(), LEGAL_NAME: created.hex()}
    assert current == expected


def test_the_cli_queues_a_backfill(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[Any, ...]] = []

    async def queue(domain: str | None, branch: str | None, recompute: bool) -> int:
        calls.append((domain, branch, recompute))
        return 7

    monkeypatch.setattr(jobs, "_queue_lineage_backfill", queue)
    assert jobs.main(["lineage-backfill", "--recompute"]) == 0
    assert jobs.main(["lineage-backfill", "--domain", DOMAIN, "--branch", "main"]) == 0
    assert calls == [(None, None, True), (DOMAIN, "main", False)]
    assert json.loads(capsys.readouterr().out.splitlines()[0]) == {"job": 7}
    with pytest.raises(SystemExit):
        jobs.main(["lineage-backfill", "--domain", DOMAIN])
