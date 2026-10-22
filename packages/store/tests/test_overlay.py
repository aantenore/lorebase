"""Overlay branches (design 6.11 mode `overlay`): reads equal to a full copy at every step,
copy on write from the base, promotion, re-fork, garbage collection and branch modes."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import (
    AggregateDerivation,
    AggregateFunction,
    Derivation,
    Kind,
    LinkDirection,
    Schema,
)
from lorebase_core.types import TypeSpec
from lorebase_core.validate import DataChange, LinkDelete, RecordDelete
from lorebase_store.commit import SchemaUpsert, commit, create_domain
from lorebase_store.gc import collect_garbage
from lorebase_store.indexes import existing_indexes
from lorebase_store.jobs import MATERIALIZE_JOB, claim_jobs
from lorebase_store.layout import load_layout
from lorebase_store.materialize import (
    BranchMode,
    MaterializationMode,
    OverlayError,
    create_branch,
    promote_overlays_of,
    rebuild,
    refork,
    run_materialize_job,
    verify,
)
from lorebase_store.models.enums import Materialization
from lorebase_store.reads import (
    Comparison,
    Direction,
    TitleSimilarity,
    get_record,
    head_schema,
    hydrate_records,
    index_spec,
    links_of_record,
    links_to_record,
    list_records,
    query_by_attribute,
    search_records,
)
from lorebase_store.repositories import MaterializedRepository, RefRepository, scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import MaterializeSettings, ReadSettings, StoreSettings
from lorebase_store.traverse import neighborhood

from .read_support import SOURCED_FROM, part_with, rated_supplier, seed_graph
from .support import (
    AUTHOR,
    DOMAIN,
    ORIGIN,
    PART,
    RATING,
    SUPPLIED_BY,
    SUPPLIER,
    VAT,
    Clock,
    attribute,
    domain,
    ident,
    supplied,
    supplier,
)

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]

OVERLAY = "agent/task-1"
COPY = "copy-1"
PART_COUNT = ident("att", 199)
PART_COUNT_DERIVATION = ident("drv", 1)


def counted_supplier() -> Kind:
    """The rated Supplier with a derived `part_count` (Parts supplied, through the inverse of
    `supplied_by`)."""
    kind = rated_supplier()
    count = attribute(
        PART_COUNT, "part_count", TypeSpec.of("integer"), derivation_id=PART_COUNT_DERIVATION
    )
    return kind.model_copy(update={"attributes": {**kind.attributes, PART_COUNT: count}})


def part_count() -> Derivation:
    return Derivation(
        id=PART_COUNT_DERIVATION,
        api_name="part_count",
        description="Parts a supplier supplies.",
        definition=AggregateDerivation(
            kind=SUPPLIER,
            attribute=PART_COUNT,
            relation=SUPPLIED_BY,
            direction=LinkDirection.INVERSE,
            function=AggregateFunction.COUNT,
        ),
    )


def settings(**materialize: Any) -> StoreSettings:
    return StoreSettings(materialize=MaterializeSettings(**materialize))


def unlink(part_number: str, supplier_code: str) -> LinkDelete:
    return LinkDelete.model_validate(
        {"relation": SUPPLIED_BY, "from": part_number, "to": supplier_code}
    )


def drop(kind: str, key: str) -> RecordDelete:
    return RecordDelete(kind=kind, key=key)


@pytest.fixture
async def sessions(engine: AsyncEngine, runtime: StoreRuntime) -> Sessions:
    """`main` with the small graph and a derived part count on Suppliers."""
    return await seeded_sessions(engine, runtime)


async def seeded_sessions(engine: AsyncEngine, runtime: StoreRuntime) -> Sessions:
    """Sessions on `main` with the small graph and a derived part count on Suppliers."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=Clock())
    async with factory() as session:
        await seed_graph(session, runtime)
    async with factory() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            [],
            schema_changes=[SchemaUpsert(counted_supplier()), SchemaUpsert(part_count())],
            author=AUTHOR,
            message="count parts",
            origin=ORIGIN,
            runtime=runtime,
        )
    return factory


async def put(
    sessions: Sessions,
    runtime: StoreRuntime,
    branches: Sequence[str],
    changes: Sequence[DataChange],
    *,
    schema_changes: Sequence[SchemaUpsert] = (),
) -> None:
    """The same commit on each branch."""
    for branch in branches:
        async with sessions() as session:
            await commit(
                session,
                DOMAIN,
                branch,
                list(changes),
                schema_changes=list(schema_changes),
                author=AUTHOR,
                message=f"change {branch}",
                origin=ORIGIN,
                runtime=runtime,
            )


async def branch_off(
    sessions: Sessions,
    runtime: StoreRuntime,
    name: str,
    mode: BranchMode | None,
    *,
    source: str = "main",
) -> MaterializationMode:
    async with sessions() as session:
        created = await create_branch(
            session,
            DOMAIN,
            name,
            created_by="usr_test",
            source=source,
            mode=mode,
            runtime=runtime,
        )
    return created.mode


def _record(row: Any) -> tuple[Any, ...]:
    """What a branch's reads must agree on; Revision ids and provenance ids differ between
    branches that receive the same changes."""
    return (row.kind_id, row.key, row.values, row.title, row.object_hash)


def _link(link: Any) -> tuple[Any, ...]:
    return (link.relation_id, link.from_key, link.to_key, link.values, link.object_hash)


async def snapshot(
    session: AsyncSession, branch: str, schema: Schema, runtime: StoreRuntime
) -> dict[str, Any]:
    """Every read primitive over every Record of the branch."""
    found: dict[str, Any] = {}
    keys: dict[str, list[str]] = {}
    for kind_id in sorted(schema.kinds):
        pages: list[tuple[Any, ...]] = []
        after: str | None = None
        while True:
            page = await list_records(
                session, DOMAIN, branch, kind_id, limit=2, after=after, runtime=runtime
            )
            if not page:
                break
            pages.extend(_record(r) for r in page)
            after = page[-1].key
        found[f"list {kind_id}"] = pages
        keys[kind_id] = [row[1] for row in pages]
    probes = {SUPPLIER: ["S-1", "S-2", "S-3", "S-4", "S-9"], PART: ["P-1", "P-3", "P-4", "P-5"]}
    for kind_id, extra in probes.items():
        for key in sorted({*keys[kind_id], *extra}):
            row = await get_record(session, DOMAIN, branch, kind_id, key)
            found[f"get {kind_id} {key}"] = None if row is None else _record(row)
            links = await links_of_record(
                session, DOMAIN, branch, schema, kind_id, key, limit=50, runtime=runtime
            )
            found[f"links {kind_id} {key}"] = [_link(link) for link in links]
            if kind_id == SUPPLIER:
                for relation_id in (SUPPLIED_BY, SOURCED_FROM):
                    inbound = await links_to_record(
                        session,
                        DOMAIN,
                        branch,
                        schema.relations[relation_id],
                        key,
                        limit=50,
                        runtime=runtime,
                    )
                    found[f"in {relation_id} {key}"] = [_link(link) for link in inbound]
    identities = [(k, key) for k, extra in probes.items() for key in extra]
    hydrated = await hydrate_records(session, DOMAIN, branch, identities, runtime=runtime)
    found["hydrate"] = sorted(_record(row) for row in hydrated.values())
    rating = index_spec(schema, SUPPLIER, RATING)
    vat = index_spec(schema, SUPPLIER, VAT)
    assert rating is not None
    assert vat is not None
    for spec, comparison, value in (
        (rating, Comparison.EQ, "7"),
        (rating, Comparison.GTE, "6"),
        (rating, Comparison.LT, "100"),
        (vat, Comparison.EQ, "IT2"),
    ):
        rows = await query_by_attribute(
            session, DOMAIN, branch, spec, comparison, value, limit=50, runtime=runtime
        )
        found[f"query {spec.attribute_id} {comparison} {value}"] = [_record(r) for r in rows]
    for words in ("bearing", "seal", "Alpha", "Delta"):
        hits = await search_records(session, DOMAIN, branch, words, limit=20, runtime=runtime)
        found[f"search {words}"] = [(h.kind_id, h.key, h.title, round(h.score, 6)) for h in hits]
    typo = await search_records(
        session,
        DOMAIN,
        branch,
        "Bearngs",
        limit=20,
        titles=TitleSimilarity(threshold=0.2, weight=0.5),
        runtime=runtime,
    )
    found["search typo"] = [(h.kind_id, h.key, h.title, round(h.score, 6)) for h in typo]
    for kind_id, key in ((PART, "P-1"), (SUPPLIER, "S-1")):
        hood = await neighborhood(
            session, DOMAIN, branch, schema, [(kind_id, key)], depth=2, runtime=runtime
        )
        found[f"hood {key}"] = (
            [(n.depth, _record(n.record)) for n in hood.records],
            [_link(link) for link in hood.links],
            hood.truncated,
        )
    repository = MaterializedRepository(session, DOMAIN, branch, writes=runtime.settings.writes)
    found["record counts"] = await repository.record_counts()
    found["link counts"] = await repository.link_counts()
    derived: dict[str, Any] = {}
    for kind_id in sorted(schema.kinds):
        async for rows in repository.stream_records(kind_id, batch=2):
            derived.update({f"{kind_id} {r.key}": r.derived for r in rows})
    found["derived"] = derived
    return found


def runtimes() -> list[StoreRuntime]:
    """The reads through the security definer functions and as plain SQL."""
    return [
        StoreRuntime.from_settings(StoreSettings(reads=ReadSettings(use_definer_functions=flag)))
        for flag in (True, False)
    ]


async def assert_same_reads(sessions: Sessions, runtime: StoreRuntime) -> None:
    """The overlay reads like the full copy, both verify against their trees, and so does
    the base."""
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = await head_schema(session, DOMAIN, OVERLAY, runtime=runtime)
        assert found is not None
        for reader in runtimes():
            overlay = await snapshot(session, OVERLAY, found.schema, reader)
            copy = await snapshot(session, COPY, found.schema, reader)
            assert overlay == copy
    for branch in ("main", OVERLAY, COPY):
        async with sessions() as session:
            report = await verify(session, DOMAIN, branch, runtime=runtime, deep=True, derived=True)
        assert report.ok, (branch, report.divergences)


async def materialization(sessions: Sessions, branch: str) -> Materialization:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        return (await RefRepository(session, DOMAIN).require(branch)).materialization


async def own_rows(sessions: Sessions, branch: str) -> int:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = await session.execute(
            text(
                "SELECT (SELECT count(*) FROM mat_records WHERE domain_id = :d AND branch = :b) "
                "+ (SELECT count(*) FROM mat_links WHERE domain_id = :d AND branch = :b)"
            ),
            {"d": DOMAIN, "b": branch},
        )
        return int(found.scalar_one())


async def test_overlay_reads_equal_a_full_copy_through_base_and_overlay_commits(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    untouched = [part_with(f"P-{n}", f"Stock part {n}") for n in range(100, 120)]
    await put(sessions, runtime, ["main"], untouched)
    assert await branch_off(sessions, runtime, OVERLAY, BranchMode.OVERLAY) is (
        MaterializationMode.OVERLAY
    )
    assert await branch_off(sessions, runtime, COPY, BranchMode.COPY) is MaterializationMode.COPY
    assert await own_rows(sessions, OVERLAY) == 0
    await assert_same_reads(sessions, runtime)

    # The base moves: rows the overlay reads change or go; it keeps their state at its fork.
    await put(
        sessions,
        runtime,
        ["main"],
        [
            supplier("S-1", "Alpha Bearings Renamed", vat="IT1", rating="6"),
            unlink("P-4", "S-2"),
            drop(PART, "P-4"),
            supplier("S-4", "Delta Springs", vat="IT4", rating="3"),
        ],
    )
    await assert_same_reads(sessions, runtime)

    # Both branches change: updates, a new Part and Link, a removed Link and Record.
    await put(
        sessions,
        runtime,
        [OVERLAY, COPY],
        [
            supplier("S-2", "Beta Seals", vat="IT2", rating="8"),
            part_with("P-5", "Spring washer", main_supplier="S-3"),
            supplied("P-5", "S-3"),
            unlink("P-2", "S-1"),
            supplied("P-2", "S-3"),
            unlink("P-3", "S-1"),
            drop(PART, "P-3"),
        ],
    )
    await assert_same_reads(sessions, runtime)

    # The base changes rows the overlay already shadows, and rows it still reads.
    await put(
        sessions,
        runtime,
        ["main"],
        [
            supplier("S-2", "Beta Seals Main", vat="IT2", rating="1"),
            part_with("P-1", "Tapered roller bearing XL", main_supplier="S-1"),
            supplier("S-3", "Gamma Filters", vat="IT3", rating="2"),
        ],
    )
    await assert_same_reads(sessions, runtime)

    # A tombstoned Record comes back; a Record the base changed after the fork changes here.
    await put(
        sessions,
        runtime,
        [OVERLAY, COPY],
        [
            part_with("P-3", "Ball bearing, sealed"),
            supplied("P-3", "S-2"),
            supplier("S-1", "Alpha Bearings Group", vat="IT1", rating="5"),
        ],
    )
    await assert_same_reads(sessions, runtime)
    assert await materialization(sessions, OVERLAY) is Materialization.OVERLAY
    # Only the rows the overlay or its base changed are stored for it.
    assert 0 < await own_rows(sessions, OVERLAY) < await own_rows(sessions, COPY)


async def test_overlay_history_reads_the_base_up_to_the_fork(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await branch_off(sessions, runtime, OVERLAY, BranchMode.OVERLAY)
    await branch_off(sessions, runtime, COPY, BranchMode.COPY)
    await put(sessions, runtime, ["main"], [supplier("S-1", "After fork", vat="IT1")])
    await put(sessions, runtime, [OVERLAY, COPY], [supplier("S-1", "On the branch", vat="IT1")])
    partition = f"k:{SUPPLIER}"
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        writes = runtime.settings.writes
        histories = {
            branch: [
                (h.old_hash, h.new_hash)
                for h in await MaterializedRepository(
                    session, DOMAIN, branch, writes=writes
                ).history(partition, "S-1")
            ]
            for branch in (OVERLAY, COPY, "main")
        }
    assert histories[OVERLAY] == histories[COPY]
    assert len(histories[OVERLAY]) == 2
    assert histories["main"][:1] == histories[OVERLAY][:1]
    assert histories["main"][1] != histories[OVERLAY][1]


async def test_overlay_is_promoted_when_it_holds_too_many_rows(
    sessions: Sessions, engine: AsyncEngine
) -> None:
    runtime = StoreRuntime.from_settings(settings(overlay_max_rows=3))
    await branch_off(sessions, runtime, OVERLAY, BranchMode.OVERLAY)
    await branch_off(sessions, runtime, COPY, BranchMode.COPY)
    await put(sessions, runtime, [OVERLAY, COPY], [supplier("S-2", "Beta", vat="IT2")])
    assert await materialization(sessions, OVERLAY) is Materialization.OVERLAY
    await put(
        sessions,
        runtime,
        [OVERLAY, COPY],
        [part_with("P-5", "Spring"), supplied("P-5", "S-3"), unlink("P-2", "S-1")],
    )
    assert await materialization(sessions, OVERLAY) is Materialization.FULL
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        ref = await RefRepository(session, DOMAIN).require(OVERLAY)
        indexes = [i for i in await existing_indexes(session, DOMAIN) if i.branch == OVERLAY]
    assert ref.base_branch is None
    assert ref.base_revision is None
    assert indexes, "a promoted overlay gets the per-Attribute indexes of a full branch"
    assert await own_rows(sessions, OVERLAY) == await own_rows(sessions, COPY)
    await assert_same_reads(sessions, runtime)
    # Promoted, it no longer depends on the base.
    await put(sessions, runtime, ["main"], [supplier("S-1", "Main only", vat="IT1")])
    await assert_same_reads(sessions, runtime)


async def test_schema_change_promotes_an_overlay_first(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await branch_off(sessions, runtime, OVERLAY, BranchMode.OVERLAY)
    await branch_off(sessions, runtime, COPY, BranchMode.COPY)
    kind = counted_supplier()
    attributes = dict(kind.attributes)
    attributes[VAT] = attributes[VAT].model_copy(update={"description": "Tax number."})
    await put(
        sessions,
        runtime,
        [OVERLAY, COPY],
        [],
        schema_changes=[SchemaUpsert(kind.model_copy(update={"attributes": attributes}))],
    )
    assert await materialization(sessions, OVERLAY) is Materialization.FULL
    await assert_same_reads(sessions, runtime)


async def test_base_lag_queues_a_promotion_and_the_job_runs_it(
    sessions: Sessions, engine: AsyncEngine
) -> None:
    runtime = StoreRuntime.from_settings(settings(overlay_max_base_lag_revisions=1))
    await branch_off(sessions, runtime, OVERLAY, BranchMode.OVERLAY)
    await branch_off(sessions, runtime, COPY, BranchMode.COPY)
    await put(sessions, runtime, ["main"], [supplier("S-1", "One", vat="IT1")])
    async with sessions() as session, session.begin():
        assert not await claim_jobs(session, "w", types=[MATERIALIZE_JOB])
    await put(sessions, runtime, ["main"], [supplier("S-1", "Two", vat="IT1")])
    async with sessions() as session, session.begin():
        jobs = await claim_jobs(session, "w", types=[MATERIALIZE_JOB], limit=5)
    assert [(job.domain_id, job.payload) for job in jobs] == [(DOMAIN, {"branch": OVERLAY})]
    assert await materialization(sessions, OVERLAY) is Materialization.OVERLAY
    await assert_same_reads(sessions, runtime)
    result = await run_materialize_job(engine, jobs[0], runtime=runtime)
    assert (result["branch"], result["status"], result["promoted"]) == (OVERLAY, "done", True)
    assert await materialization(sessions, OVERLAY) is Materialization.FULL
    await assert_same_reads(sessions, runtime)


async def test_lagging_overlay_is_promoted_by_its_own_commit(
    sessions: Sessions, engine: AsyncEngine
) -> None:
    runtime = StoreRuntime.from_settings(settings(overlay_max_base_lag_revisions=1))
    await branch_off(sessions, runtime, OVERLAY, BranchMode.OVERLAY)
    await branch_off(sessions, runtime, COPY, BranchMode.COPY)
    await put(sessions, runtime, ["main"], [supplier("S-1", "One", vat="IT1")])
    await put(sessions, runtime, ["main"], [supplier("S-1", "Two", vat="IT1")])
    await put(sessions, runtime, [OVERLAY, COPY], [supplier("S-2", "Beta", vat="IT2")])
    assert await materialization(sessions, OVERLAY) is Materialization.FULL
    await assert_same_reads(sessions, runtime)


async def test_garbage_collection_keeps_what_overlays_read(
    sessions: Sessions, engine: AsyncEngine, runtime: StoreRuntime
) -> None:
    await branch_off(sessions, runtime, "dev", BranchMode.COPY)
    await put(sessions, runtime, ["dev"], [supplier("S-1", "Dev only", vat="IT1")])
    await branch_off(sessions, runtime, OVERLAY, BranchMode.OVERLAY)
    await branch_off(sessions, runtime, COPY, BranchMode.COPY)
    for step in range(3):
        await put(sessions, runtime, ["main"], [supplier("S-1", f"Main {step}", vat="IT1")])
    await put(sessions, runtime, [OVERLAY, COPY], [supplier("S-2", "Beta", vat="IT2")])
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await RefRepository(session, DOMAIN).delete("dev")
    later = datetime.now(UTC) + timedelta(days=30)
    report = await collect_garbage(engine, DOMAIN, runtime=runtime, now=later, grace_seconds=0)
    assert report.revisions == 1, "the commit of the deleted branch goes"
    await assert_same_reads(sessions, runtime)
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        writes = runtime.settings.writes
        history = await MaterializedRepository(session, DOMAIN, OVERLAY, writes=writes).history(
            f"k:{SUPPLIER}", "S-1"
        )
    assert len(history) == 1, "the base history up to the fork survives the collection"


async def test_refork_moves_a_merged_overlay_to_the_base_head(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await branch_off(sessions, runtime, OVERLAY, BranchMode.OVERLAY)
    await put(sessions, runtime, [OVERLAY], [supplier("S-2", "Beta on agent", vat="IT2")])
    async with sessions() as session:
        with pytest.raises(OverlayError, match="merge it first"):
            await refork(session, DOMAIN, OVERLAY, actor="usr_test", runtime=runtime)
    # Fresh overlay with no own commits: the base moves, the overlay catches up cheaply.
    await branch_off(sessions, runtime, "agent/task-2", BranchMode.OVERLAY)
    await put(sessions, runtime, ["main"], [supplier("S-1", "Moved on", vat="IT1")])
    async with sessions() as session:
        moved = await refork(session, DOMAIN, "agent/task-2", actor="usr_test", runtime=runtime)
        await session.commit()
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        main = await RefRepository(session, DOMAIN).require("main")
        record = await get_record(session, DOMAIN, "agent/task-2", SUPPLIER, "S-1")
        log = await RefRepository(session, DOMAIN).log("agent/task-2")
    assert moved.head == main.head
    assert moved.base_revision == main.head
    assert moved.materialization is Materialization.OVERLAY
    assert record is not None
    assert record.title == "Moved on"
    assert log[-1].reason == "branch.refork"
    assert await own_rows(sessions, "agent/task-2") == 0
    async with sessions() as session:
        report = await verify(session, DOMAIN, "agent/task-2", runtime=runtime, derived=True)
    assert report.ok, report.divergences


async def test_deleting_or_rebuilding_a_base_promotes_its_overlays(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await branch_off(sessions, runtime, "base", BranchMode.COPY)
    await branch_off(sessions, runtime, OVERLAY, BranchMode.OVERLAY, source="base")
    await branch_off(sessions, runtime, "agent/task-2", BranchMode.OVERLAY, source="base")
    await put(sessions, runtime, [OVERLAY], [supplier("S-2", "Beta", vat="IT2")])
    async with sessions() as session:
        await rebuild(session, DOMAIN, "base", runtime=runtime)
        await session.commit()
    assert await materialization(sessions, OVERLAY) is Materialization.FULL
    assert await materialization(sessions, "agent/task-2") is Materialization.FULL
    await branch_off(sessions, runtime, "agent/task-3", BranchMode.OVERLAY, source="base")
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        refs = RefRepository(session, DOMAIN)
        await refs.lock("base", timeout_ms=0)
        assert await promote_overlays_of(session, DOMAIN, "base", runtime=runtime) == [
            "agent/task-3"
        ]
        assert await refs.delete("base")
    for branch in (OVERLAY, "agent/task-2", "agent/task-3"):
        async with sessions() as session:
            report = await verify(session, DOMAIN, branch, runtime=runtime, deep=True)
        assert report.ok, (branch, report.divergences)


async def test_branch_mode_defaults_and_fallbacks(sessions: Sessions) -> None:
    runtime = StoreRuntime.from_settings(settings(overlay_max_per_branch=2))
    assert await branch_off(sessions, runtime, "agent/a", None) is MaterializationMode.OVERLAY
    assert await branch_off(sessions, runtime, "extract/b", None) is MaterializationMode.OVERLAY
    # The base has its two overlays: the next branch is a copy.
    assert await branch_off(sessions, runtime, "source/c", None) is MaterializationMode.COPY
    assert await branch_off(sessions, runtime, "feature", None) is MaterializationMode.COPY
    # An overlay source gives a copy of its resolved rows; so does an explicit copy.
    assert (
        await branch_off(sessions, runtime, "agent/d", BranchMode.OVERLAY, source="agent/a")
        is MaterializationMode.COPY
    )
    everywhere = StoreRuntime.from_settings(settings(branch_mode="overlay"))
    assert await branch_off(sessions, everywhere, "feature-2", None) is (
        MaterializationMode.OVERLAY
    )
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        layout = await load_layout(session, DOMAIN, "agent/a")
        main = await RefRepository(session, DOMAIN).require("main")
        overlay = await RefRepository(session, DOMAIN).require("agent/a")
    assert layout.overlay
    assert layout.base == "main"
    assert overlay.base_revision == main.head
    for branch in ("agent/a", "agent/d", "source/c", "feature-2"):
        async with sessions() as session:
            report = await verify(session, DOMAIN, branch, runtime=runtime, deep=True)
        assert report.ok, (branch, report.divergences)


async def test_reads_on_a_stale_layout_after_promotion_stay_correct(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    """A reader that resolved the layout before a promotion (READ COMMITTED) reads only the
    overlay's rows afterwards: the base half checks that the ref is still that overlay."""
    await branch_off(sessions, runtime, OVERLAY, BranchMode.OVERLAY)
    await put(sessions, runtime, [OVERLAY], [unlink("P-4", "S-2"), drop(PART, "P-4")])
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        stale = await load_layout(session, DOMAIN, OVERLAY)
    promoted = StoreRuntime.from_settings(settings(overlay_max_rows=1))
    await put(sessions, promoted, [OVERLAY], [supplier("S-2", "Beta", vat="IT2")])
    assert await materialization(sessions, OVERLAY) is Materialization.FULL
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        assert await get_record(session, DOMAIN, stale, PART, "P-4") is None
        page = await list_records(session, DOMAIN, stale, PART, limit=10, runtime=runtime)
    assert [row.key for row in page] == ["P-1", "P-2", "P-3"]


async def test_overlay_neighborhood_follows_both_directions(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    await branch_off(sessions, runtime, OVERLAY, BranchMode.OVERLAY)
    await put(sessions, runtime, [OVERLAY], [unlink("P-4", "S-2"), supplied("P-4", "S-3")])
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = await head_schema(session, DOMAIN, OVERLAY, runtime=runtime)
        assert found is not None
        hood = await neighborhood(
            session,
            DOMAIN,
            OVERLAY,
            found.schema,
            [(SUPPLIER, "S-3")],
            depth=1,
            direction=Direction.IN,
            runtime=runtime,
        )
    assert sorted(n.record.key for n in hood.records) == ["P-4", "S-3"]
