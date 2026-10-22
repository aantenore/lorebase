"""Full rebuild, branch creation and verification of materialized heads."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import ImportOrigin, ProvenanceEntry
from lorebase_core.tree import kind_partition, relation_partition
from lorebase_core.types import TypeSpec
from lorebase_core.validate import RecordDelete
from lorebase_store.commit import Change, CommitResult, SchemaUpsert, commit, create_domain
from lorebase_store.materialize import (
    BranchExistsError,
    DivergenceReason,
    MaterializationMode,
    create_branch,
    rebuild,
    verify,
)
from lorebase_store.runtime import StoreRuntime

from .support import (
    AUTHOR,
    DOMAIN,
    ORIGIN,
    SUPPLIED_BY,
    SUPPLIER,
    Clock,
    domain,
    part,
    schema_changes,
    supplied,
    supplier,
    supplier_kind,
)

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]

SNAPSHOT_QUERIES = {
    "records": (
        "SELECT kind_id, key, values, derived, title, search::text, object_hash, lineage, "
        "lineage_revisions, updated_revision, updated_at FROM mat_records WHERE branch = :b "
        "ORDER BY kind_id, key"
    ),
    "links": (
        "SELECT relation_id, from_key, to_key, values, object_hash, lineage "
        "FROM mat_links WHERE branch = :b ORDER BY relation_id, from_key, to_key"
    ),
    "history": (
        "SELECT partition, element_key, revision_id, at, old_hash, new_hash, provenance "
        "FROM mat_history WHERE branch = :b ORDER BY partition, element_key, at"
    ),
    "schema": "SELECT schema_hash, compiled FROM mat_schema WHERE branch = :b",
}


@pytest.fixture
async def sessions(engine: AsyncEngine, runtime: StoreRuntime) -> Sessions:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=Clock())
    return factory


async def snapshot(sessions: Sessions, branch: str) -> dict[str, list[Any]]:
    """Every materialized row of a branch, row by row."""
    async with sessions() as session:
        return {
            name: [tuple(row) for row in await session.execute(text(sql), {"b": branch})]
            for name, sql in SNAPSHOT_QUERIES.items()
        }


async def run(
    sessions: Sessions,
    runtime: StoreRuntime,
    clock: Clock,
    changes: list[Any],
    branch: str = "main",
    **options: Any,
) -> CommitResult:
    async with sessions() as session:
        return await commit(
            session,
            DOMAIN,
            branch,
            changes,
            author=AUTHOR,
            message="test",
            origin=ORIGIN,
            runtime=runtime,
            clock=clock,
            **options,
        )


async def history_of_commits(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock
) -> list[CommitResult]:
    """Schema, inserts, updates with provenance, deletes with cascade, a schema migration."""
    entry = ProvenanceEntry(
        id="prv_01JB8Z00000000000000000042",
        origin=ImportOrigin(batch_id="b42"),
        actor="usr_import",
        at=clock(),
    )
    return [
        await run(
            sessions,
            runtime,
            clock,
            [
                supplier("S-1", "Acme Bearings", country="IT", rating="4"),
                supplier("S-2", "Bolt Works", country="DE", rating="5", vat="DE2"),
                part("P-1", "Bearing"),
                part("P-2", "Bolt"),
                supplied("P-1", "S-1", primary=True),
                supplied("P-2", "S-2"),
            ],
            schema_changes=schema_changes(),
        ),
        await run(
            sessions,
            runtime,
            clock,
            [
                Change(supplier("S-1", "Acme Bearings", country="FR", rating="4"), entry.id),
                RecordDelete(kind=SUPPLIER, key="S-2"),
            ],
            provenance=[entry],
        ),
        await run(
            sessions,
            runtime,
            clock,
            [],
            schema_changes=[SchemaUpsert(supplier_kind(rating=TypeSpec.decimal(4, 1)))],
        ),
        await run(
            sessions,
            runtime,
            clock,
            [supplier("S-3", "Cog Ltd", country="ES"), supplied("P-2", "S-3")],
        ),
    ]


async def assert_verified(sessions: Sessions, runtime: StoreRuntime, branch: str) -> None:
    async with sessions() as session:
        report = await verify(session, DOMAIN, branch, runtime=runtime, deep=True)
    assert report.ok, report.divergences
    assert report.rows > 0


async def test_rebuild_equals_incremental_materialization(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    await history_of_commits(sessions, runtime, clock)
    incremental = await snapshot(sessions, "main")
    assert len(incremental["records"]) == 4
    assert len(incremental["links"]) == 2
    await assert_verified(sessions, runtime, "main")
    async with sessions() as session, session.begin():
        report = await rebuild(session, DOMAIN, "main", runtime=runtime)
    assert (report.records, report.links) == (4, 2)
    assert await snapshot(sessions, "main") == incremental


async def test_branch_creation_copy_and_rebuild_from_tree(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    clock = Clock()
    first = await run(
        sessions,
        runtime,
        clock,
        [supplier("S-1", "Acme Bearings", country="IT"), part("P-1", "Bearing")],
        schema_changes=schema_changes(),
    )
    at_first = await snapshot(sessions, "main")
    await history_of_commits(sessions, runtime, clock)
    current = await snapshot(sessions, "main")

    async with sessions() as session:
        copied = await create_branch(
            session, DOMAIN, "dev", created_by=AUTHOR.principal, runtime=runtime
        )
    assert copied.mode is MaterializationMode.COPY
    assert await snapshot(sessions, "dev") == current

    async with sessions() as session:
        rebuilt = await create_branch(
            session,
            DOMAIN,
            "audit/first",
            created_by=AUTHOR.principal,
            start=first.revision_id,
            runtime=runtime,
        )
    assert rebuilt.mode is MaterializationMode.REBUILD
    assert rebuilt.ref.head == first.revision_id
    assert await snapshot(sessions, "audit/first") == at_first

    with pytest.raises(BranchExistsError):
        async with sessions() as session:
            await create_branch(session, DOMAIN, "dev", created_by="usr_t", runtime=runtime)

    # The new branch takes commits; main does not move.
    on_dev = await run(sessions, runtime, clock, [part("P-9", "Washer")], branch="dev")
    assert on_dev.parents == (copied.ref.head,)
    assert await snapshot(sessions, "main") == current
    for branch in ("main", "dev", "audit/first"):
        await assert_verified(sessions, runtime, branch)


async def test_verify_reports_divergences(sessions: Sessions, runtime: StoreRuntime) -> None:
    await history_of_commits(sessions, runtime, Clock())
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE mat_records SET object_hash = sha256('x'::bytea) "
                "WHERE key = 'S-1' AND branch = 'main'"
            )
        )
        await session.execute(
            text("UPDATE mat_records SET title = 'Wrong' WHERE key = 'P-1' AND branch = 'main'")
        )
        await session.execute(
            text("DELETE FROM mat_links WHERE to_key = 'S-3' AND branch = 'main'")
        )
        await session.execute(
            text(
                "INSERT INTO mat_records (domain_id, branch, kind_id, key, values, object_hash, "
                "lineage, updated_revision, updated_at) SELECT domain_id, branch, kind_id, "
                "'S-404', values, object_hash, lineage, updated_revision, updated_at "
                "FROM mat_records WHERE key = 'S-3' AND branch = 'main'"
            )
        )
    async with sessions() as session:
        shallow = await verify(session, DOMAIN, "main", runtime=runtime)
        deep = await verify(session, DOMAIN, "main", runtime=runtime, deep=True)
    found = {(d.reason, d.partition, d.key) for d in shallow.divergences}
    assert found == {
        (DivergenceReason.HASH_MISMATCH, kind_partition(SUPPLIER), "S-1"),
        (DivergenceReason.EXTRA_ROW, kind_partition(SUPPLIER), "S-404"),
        (DivergenceReason.COUNT_MISMATCH, kind_partition(SUPPLIER), None),
        (DivergenceReason.MISSING_ROW, relation_partition(SUPPLIED_BY), "P-2\x1fS-3"),
        (DivergenceReason.COUNT_MISMATCH, relation_partition(SUPPLIED_BY), None),
    }
    assert not shallow.ok
    assert DivergenceReason.VALUES_MISMATCH in {d.reason for d in deep.divergences}
    async with sessions() as session, session.begin():
        await rebuild(session, DOMAIN, "main", runtime=runtime)
    await assert_verified(sessions, runtime, "main")
