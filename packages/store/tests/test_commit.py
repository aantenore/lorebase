"""Commit pipeline against Postgres (design 6.6)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from lorebase_core.model import ImportOrigin, ProvenanceEntry
from lorebase_core.tree import kind_partition, tree_get
from lorebase_core.types import TypeSpec
from lorebase_core.validate import IssueCode, RecordDelete
from lorebase_store.commit import (
    BranchLockTimeoutError,
    Change,
    CommitResult,
    SchemaUpsert,
    StaleHeadError,
    ValidationFailedError,
    commit,
    create_domain,
)
from lorebase_store.materialize import verify
from lorebase_store.materialize.rows import ELEMENT_PROVENANCE_KEY
from lorebase_store.models import (
    AuditEntry,
    MatHistory,
    MatLink,
    MatRecord,
    OutboxEvent,
    Provenance,
    Revision,
)
from lorebase_store.nodestore import PgNodeStore
from lorebase_store.repositories import MaterializedRepository, RefRepository
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import CommitSettings, StoreSettings

from .support import (
    AUTHOR,
    COUNTRY,
    DOMAIN,
    IS_PRIMARY,
    LEGAL_NAME,
    ORIGIN,
    PART,
    RATING,
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
LOCK_WAIT_TIMEOUT_S = 10.0


@pytest.fixture
async def sessions(engine: AsyncEngine, runtime: StoreRuntime) -> AsyncIterator[Sessions]:
    """Sessions on a database holding the `supply` Domain with an empty `main` branch.

    At teardown the materialized head of `main` must still match its tree.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=Clock())
    yield factory
    async with factory() as session:
        report = await verify(session, DOMAIN, "main", runtime=runtime, deep=True)
    assert report.ok, report.divergences


@pytest.fixture
def clock() -> Clock:
    return Clock()


async def seed(sessions: Sessions, runtime: StoreRuntime, clock: Clock) -> CommitResult:
    """Schema, two Suppliers, two Parts and two Links on `main`."""
    async with sessions() as session:
        return await commit(
            session,
            DOMAIN,
            "main",
            [
                supplier("S-1", "Acme Bearings", country="IT", rating="4", vat="IT1"),
                supplier("S-2", "Bolt Works", country="DE", rating="5"),
                part("P-1", "Bearing"),
                part("P-2", "Bolt"),
                supplied("P-1", "S-1", primary=True),
                supplied("P-2", "S-2"),
            ],
            author=AUTHOR,
            message="Seed supply",
            origin=ORIGIN,
            schema_changes=schema_changes(),
            runtime=runtime,
            clock=clock,
        )


async def count(session: AsyncSession, model: type, **filters: object) -> int:
    query = select(func.count()).select_from(model)
    for name, expected in filters.items():
        query = query.where(getattr(model, name) == expected)
    return int(await session.scalar(query) or 0)


async def test_commit_schema_records_and_links(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock
) -> None:
    result = await seed(sessions, runtime, clock)
    assert not result.no_op
    assert result.generation == 2
    assert result.ref_version == 2
    assert len(result.changeset) == 3 + 6  # three schema elements, four Records, two Links
    async with sessions() as session:
        ref = await RefRepository(session, DOMAIN).require("main")
        assert (ref.head, ref.version) == (result.revision_id, 2)
        assert await count(session, MatRecord, branch="main") == 4
        assert await count(session, MatLink, branch="main") == 2
        assert await count(session, MatHistory, branch="main") == 1 + 9
        assert await count(session, Revision) == 2
        assert await count(session, AuditEntry) == 2
        assert await count(session, OutboxEvent, event_type="revision.committed") == 2

        acme = await session.get(MatRecord, (DOMAIN, "main", SUPPLIER, "S-1"))
        assert acme is not None
        assert acme.title == "Acme Bearings"
        assert acme.values[COUNTRY] == "IT"
        assert acme.updated_revision == result.revision_id
        assert acme.search is not None
        own = set(acme.lineage.values())
        assert len(own) == 1
        assert set(acme.lineage) == set(acme.values)
        provenance = await session.get(Provenance, (DOMAIN, own.pop()))
        assert provenance is not None
        assert provenance.origin_type == ORIGIN.type
        link = await session.get(MatLink, (DOMAIN, "main", SUPPLIED_BY, "P-1", "S-1"))
        assert link is not None
        assert link.values == {IS_PRIMARY: True}

        schema = await MaterializedRepository(
            session, DOMAIN, "main", writes=runtime.settings.writes
        ).schema()
        assert schema is not None
        assert set(schema.compiled["kinds"]) == {SUPPLIER, PART}


async def test_read_your_writes(sessions: Sessions, runtime: StoreRuntime, clock: Clock) -> None:
    await seed(sessions, runtime, clock)
    async with sessions() as session, session.begin():
        result = await commit(
            session,
            DOMAIN,
            "main",
            [supplier("S-3", "Cog Ltd", country="FR")],
            author=AUTHOR,
            message="Add S-3",
            origin=ORIGIN,
            runtime=runtime,
            clock=clock,
        )
        # Inside the caller's transaction the commit is already readable.
        assert await session.get(MatRecord, (DOMAIN, "main", SUPPLIER, "S-3")) is not None
    async with sessions() as session:
        row = await session.get(MatRecord, (DOMAIN, "main", SUPPLIER, "S-3"))
        assert row is not None

        def from_tree(sync: Session) -> bytes | None:
            store = PgNodeStore(sync, DOMAIN)
            return tree_get(store, result.root, kind_partition(SUPPLIER), "S-3")

        assert await session.run_sync(from_tree) == row.object_hash


async def test_update_delete_lineage_and_cascade(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock
) -> None:
    seeded = await seed(sessions, runtime, clock)
    entry = ProvenanceEntry(
        id="prv_01JB8Z00000000000000000077",
        origin=ImportOrigin(batch_id="b7"),
        actor="usr_import",
        at=clock(),
    )
    async with sessions() as session:
        result = await commit(
            session,
            DOMAIN,
            "main",
            [
                Change(
                    supplier("S-1", "Acme Bearings", country="FR", rating="4", vat="IT1"), entry.id
                ),
                RecordDelete(kind=SUPPLIER, key="S-2"),
            ],
            author=AUTHOR,
            message="Move S-1, drop S-2",
            origin=ORIGIN,
            provenance=[entry],
            runtime=runtime,
            clock=clock,
        )
    assert result.parents == (seeded.revision_id,)
    # supplied_by cascades: the Link to S-2 is removed with it.
    assert [c.identity for c in result.derived_changes] == [(SUPPLIED_BY, "P-2", "S-2")]
    by_key = {(e.partition, e.key): e for e in result.changeset}
    assert by_key[(kind_partition(SUPPLIER), "S-1")].provenance == {COUNTRY: entry.id}
    assert set(by_key[(kind_partition(SUPPLIER), "S-2")].provenance) == {ELEMENT_PROVENANCE_KEY}
    async with sessions() as session:
        acme = await session.get(MatRecord, (DOMAIN, "main", SUPPLIER, "S-1"))
        assert acme is not None
        assert acme.lineage[COUNTRY] == entry.id
        assert acme.lineage[LEGAL_NAME] != entry.id  # unchanged Attributes keep their lineage
        assert await session.get(MatRecord, (DOMAIN, "main", SUPPLIER, "S-2")) is None
        assert await count(session, MatLink, branch="main") == 1
        history = await MaterializedRepository(
            session, DOMAIN, "main", writes=runtime.settings.writes
        ).history(kind_partition(SUPPLIER), "S-2")
        assert [h.new_hash is None for h in history] == [False, True]


async def test_validation_failure_leaves_nothing(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock
) -> None:
    seeded = await seed(sessions, runtime, clock)
    async with sessions() as session:
        with pytest.raises(ValidationFailedError) as failure:
            await commit(
                session,
                DOMAIN,
                "main",
                [
                    supplier("S-9", "Bad", country="ITA"),
                    supplier("S-10", "Dup", vat="IT1"),
                    supplied("P-1", "S-404"),
                ],
                author=AUTHOR,
                message="Invalid",
                origin=ORIGIN,
                runtime=runtime,
                clock=clock,
            )
    codes = {issue.code for issue in failure.value.issues}
    assert {IssueCode.VALUE_MAX_LENGTH, IssueCode.VALUE_UNIQUE} <= codes
    assert IssueCode.LINK_ENDPOINT_MISSING in codes
    async with sessions() as session:
        ref = await RefRepository(session, DOMAIN).require("main")
        assert ref.head == seeded.revision_id
        assert await count(session, Revision) == 2
        assert await count(session, MatRecord) == 4


async def test_no_op_commit(sessions: Sessions, runtime: StoreRuntime, clock: Clock) -> None:
    seeded = await seed(sessions, runtime, clock)
    async with sessions() as session:
        result = await commit(
            session,
            DOMAIN,
            "main",
            [part("P-1", "Bearing")],
            author=AUTHOR,
            message="Nothing new",
            origin=ORIGIN,
            runtime=runtime,
            clock=clock,
        )
    assert result.no_op
    assert result.revision_id == seeded.revision_id


async def test_stale_expected_head(sessions: Sessions, runtime: StoreRuntime, clock: Clock) -> None:
    seeded = await seed(sessions, runtime, clock)
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            [part("P-3", "Gear")],
            author=AUTHOR,
            message="Move on",
            origin=ORIGIN,
            runtime=runtime,
            clock=clock,
        )
    async with sessions() as session:
        with pytest.raises(StaleHeadError) as stale:
            await commit(
                session,
                DOMAIN,
                "main",
                [part("P-4", "Nut")],
                author=AUTHOR,
                message="From an old head",
                origin=ORIGIN,
                expected_head=seeded.revision_id,
                runtime=runtime,
                clock=clock,
            )
    assert stale.value.expected == seeded.revision_id


async def wait_for_lock_waiter(engine: AsyncEngine) -> None:
    """Return once another session waits on a lock."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LOCK_WAIT_TIMEOUT_S
    async with engine.connect() as connection:
        while loop.time() < deadline:
            waiting = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                )
            )
            if waiting:
                return
            await asyncio.sleep(0.05)
    raise AssertionError("no session waited on the branch lock")


async def test_concurrent_commits_serialize(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime, clock: Clock
) -> None:
    seeded = await seed(sessions, runtime, clock)

    async with sessions() as first:
        await first.begin()
        a = await _commit_part(first, runtime, clock, "A")
        second = asyncio.create_task(_commit_in_new_session(sessions, runtime, clock, "B"))
        await wait_for_lock_waiter(engine)
        assert not second.done()
        await first.commit()
    b = await second
    assert a.parents == (seeded.revision_id,)
    assert b.parents == (a.revision_id,)
    assert b.ref_version == a.ref_version + 1


async def _commit_in_new_session(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock, label: str
) -> CommitResult:
    async with sessions() as session:
        return await _commit_part(session, runtime, clock, label)


async def _commit_part(
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, label: str
) -> CommitResult:
    return await commit(
        session,
        DOMAIN,
        "main",
        [part(f"P-{label}", f"Part {label}")],
        author=AUTHOR,
        message=label,
        origin=ORIGIN,
        runtime=runtime,
        clock=clock,
    )


async def test_branch_lock_timeout(sessions: Sessions, runtime: StoreRuntime, clock: Clock) -> None:
    await seed(sessions, runtime, clock)
    impatient = StoreRuntime.from_settings(
        StoreSettings(commit=CommitSettings(lock_timeout_ms=100))
    )
    async with sessions() as holder, holder.begin():
        await RefRepository(holder, DOMAIN).lock("main", timeout_ms=0)
        async with sessions() as session:
            with pytest.raises(BranchLockTimeoutError):
                await commit(
                    session,
                    DOMAIN,
                    "main",
                    [part("P-9", "Late")],
                    author=AUTHOR,
                    message="Late",
                    origin=ORIGIN,
                    runtime=impatient,
                    clock=clock,
                )


async def test_schema_change_casts_existing_values(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock
) -> None:
    await seed(sessions, runtime, clock)
    async with sessions() as session:
        result = await commit(
            session,
            DOMAIN,
            "main",
            [],
            author=AUTHOR,
            message="Ratings become decimals",
            origin=ORIGIN,
            schema_changes=[SchemaUpsert(supplier_kind(rating=TypeSpec.decimal(4, 1)))],
            runtime=runtime,
            clock=clock,
        )
    migrated = {
        e.key: e.provenance for e in result.changeset if e.partition == kind_partition(SUPPLIER)
    }
    assert set(migrated) == {"S-1", "S-2"}
    async with sessions() as session:
        acme = await session.get(MatRecord, (DOMAIN, "main", SUPPLIER, "S-1"))
        assert acme is not None
        assert acme.values[RATING] == "4.0"
        origin = await session.get(Provenance, (DOMAIN, acme.lineage[RATING]))
        assert origin is not None
        assert origin.origin_type == "schema_migration"
        assert origin.origin_ref["attribute_id"] == RATING


async def test_schema_change_with_uncastable_value_fails(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock
) -> None:
    await seed(sessions, runtime, clock)
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            [supplier("S-3", "Cog Ltd", rating="excellent")],
            author=AUTHOR,
            message="Free text rating",
            origin=ORIGIN,
            runtime=runtime,
            clock=clock,
        )
    async with sessions() as session:
        with pytest.raises(ValidationFailedError) as failure:
            await commit(
                session,
                DOMAIN,
                "main",
                [],
                author=AUTHOR,
                message="Ratings become integers",
                origin=ORIGIN,
                schema_changes=[SchemaUpsert(supplier_kind(rating=TypeSpec.of("integer")))],
                runtime=runtime,
                clock=clock,
            )
    [issue] = failure.value.issues
    assert issue.code is IssueCode.VALUE_TYPE
    assert (issue.kind, issue.key, issue.attribute) == (SUPPLIER, "S-3", RATING)
    assert issue.details["class"] == "XS3"


async def test_bulk_commit_of_ten_thousand_records(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    await seed(sessions, runtime, clock)
    records = [
        supplier(f"S-{n:05d}", f"Supplier {n}", country="IT", rating=str(n % 5), vat=f"VAT{n}")
        for n in range(10, 10_010)
    ]
    async with sessions() as session:
        started = time.perf_counter()
        result = await commit(
            session,
            DOMAIN,
            "main",
            records,
            author=AUTHOR,
            message="Bulk import",
            origin=ORIGIN,
            runtime=runtime,
            clock=clock,
        )
        elapsed = time.perf_counter() - started
    assert len(result.changeset) == len(records)
    async with sessions() as session:
        assert await count(session, MatRecord, kind_id=SUPPLIER) == len(records) + 2
    with capsys.disabled():
        print(f"\n[baseline] commit of {len(records)} new Records: {elapsed:.2f} s")
