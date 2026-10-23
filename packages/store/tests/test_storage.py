"""Storage profiles (lorebase_store.storage) and shared lineage maps (revision 0036)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import Schema
from lorebase_store import migrations, storage
from lorebase_store.commit import commit, create_domain
from lorebase_store.indexes import IndexReason, existing_indexes, plan_indexes
from lorebase_store.materialize import create_branch, verify
from lorebase_store.migrations import include_object
from lorebase_store.reads import (
    Comparison,
    TypoTolerance,
    get_record,
    head_schema,
    hydrate_records,
    index_spec,
    query_by_attribute,
    search_corrections,
)
from lorebase_store.repositories import MaterializedRepository, scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StorageSettings, StoreSettings
from lorebase_store.traverse import neighborhood

from .read_support import rated_supplier, seed_graph, sourced_from, sourced_part
from .support import (
    AUTHOR,
    DOMAIN,
    ORIGIN,
    PART,
    RATING,
    SUPPLIER,
    VAT,
    Clock,
    domain,
    supplier,
)
from .test_reads import reader_role

Sessions = async_sessionmaker[AsyncSession]
DIMS = StoreSettings().embeddings.dims
REFERENCES = text(
    "SELECT count(*) FILTER (WHERE lineage ? '$map'), count(*) FROM mat_records "
    "WHERE domain_id = :d"
)
LARGE_INLINE_LINKS = text(
    "SELECT count(*) FILTER (WHERE NOT lineage ? '$map' AND pg_column_size(lineage) > 64), "
    "count(*) FILTER (WHERE lineage ? '$map') FROM mat_links WHERE domain_id = :d"
)
"""Link maps larger than a reference that are inline, and references (maps of at most 64
bytes stay inline whatever the profile)."""
MAPS = text("SELECT count(*) FROM mat_lineage_maps WHERE domain_id = :d")


def test_profiles_and_overrides() -> None:
    compact = storage.storage_choices(StorageSettings(profile="compact"))
    assert (compact.lineage_maps, compact.values_index, compact.attribute_indexes) == (
        "shared",
        False,
        "constraints",
    )
    assert storage.storage_choices(StorageSettings()) == storage.PROFILES["balanced"]
    fast = storage.storage_choices(StorageSettings(profile="fast", lineage_maps="shared"))
    assert (fast.lineage_maps, fast.embedding_precision, fast.prewarm) == ("shared", "full", True)
    assert storage.constraints_only(StoreSettings(storage=StorageSettings(profile="compact")))
    assert not storage.constraints_only(
        StoreSettings(storage=StorageSettings(profile="compact", attribute_indexes="all"))
    )


def test_constraints_only_keeps_unique_and_foreign_key_indexes() -> None:
    kinds = (rated_supplier(), sourced_part())
    schema = Schema(
        domain=domain(),
        kinds={k.id: k for k in kinds},
        relations={sourced_from().id: sourced_from()},
    )
    plan = plan_indexes(schema)
    built = plan.built(constraints_only=True)
    kept = {IndexReason.UNIQUE, IndexReason.FOREIGN_KEY}
    assert all(kept.intersection(spec.reasons) for spec in built)
    assert RATING in {s.attribute_id for s in plan.indexes}
    assert RATING not in {s.attribute_id for s in built}
    assert VAT in {s.attribute_id for s in built}
    assert plan.built(constraints_only=False) == plan.indexes


def test_schema_comparison_skips_profile_indexes() -> None:
    for name in ("ix_mat_records_values", "ix_mat_lexicon_branch_word"):
        assert not include_object(None, name, "index", True, None)
        assert not include_object(None, name, "index", False, None)


def test_hnsw_index_sql() -> None:
    assert storage.hnsw_index_sql(384, "half") == (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS embeddings_hnsw_half_384 ON embeddings "
        "USING hnsw ((embedding::halfvec(384)) halfvec_cosine_ops) WHERE dims = 384"
    )
    assert storage.hnsw_index_name(384, "full") == "embeddings_hnsw_384"


# Integration --------------------------------------------------------------------------------------


@pytest.fixture
async def sessions(engine: AsyncEngine, runtime: StoreRuntime) -> AsyncIterator[Sessions]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=Clock())
    async with factory() as session:
        await seed_graph(session, runtime)
    yield factory
    async with factory() as session:
        report = await verify(session, DOMAIN, "main", runtime=runtime, deep=True)
        assert report.ok, report


async def counts(sessions: Sessions, statement: Any) -> tuple[int, ...]:
    async with sessions() as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        return tuple((await session.execute(statement, {"d": DOMAIN})).one())


async def snapshot(sessions: Sessions, runtime: StoreRuntime) -> dict[str, Any]:
    """What the read primitives return for the seeded graph, lineage included."""
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        record = await get_record(session, DOMAIN, "main", SUPPLIER, "S-1")
        assert record is not None
        hydrated = await hydrate_records(
            session, DOMAIN, "main", [(SUPPLIER, "S-2"), (PART, "P-1")], runtime=runtime
        )
        rows = await MaterializedRepository(
            session, DOMAIN, "main", writes=runtime.settings.writes
        ).records(SUPPLIER, ["S-1", "S-3"])
        return {
            "get": (record.lineage, record.lineage_revisions),
            "hydrate": {k: v.lineage for k, v in hydrated.items()},
            "repository": {k: (v.lineage, v.lineage_revisions) for k, v in rows.items()},
        }


async def state(engine: AsyncEngine) -> storage.StorageState:
    async with engine.connect() as connection:
        return await storage.read_state(connection, DIMS)


async def apply(engine: AsyncEngine, **options: Any) -> storage.ApplyReport:
    settings = StoreSettings(storage=StorageSettings(**options))
    report = await storage.apply(engine, settings, switch_wait_s=0)
    assert report.complete, report.as_dict()
    return report


@pytest.mark.integration
async def test_lineage_maps_are_shared_and_read_back(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    """With the default profile every writer stores references; readers see the maps."""
    shared, total = await counts(sessions, REFERENCES)
    assert total > 0
    assert shared == total
    assert 0 < (await counts(sessions, MAPS))[0] < total
    assert (await counts(sessions, LARGE_INLINE_LINKS))[0] == 0
    found = await snapshot(sessions, runtime)
    lineage, _ = found["get"]
    assert lineage
    assert all(str(v).startswith("prv_") for v in lineage.values())
    stored, revisions = found["repository"]["S-1"]
    assert stored == lineage
    assert revisions
    assert "$created" in revisions
    assert "$map" not in repr(found)


@pytest.mark.integration
async def test_switching_profiles_rewrites_rows_and_keeps_reads(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime
) -> None:
    before = await snapshot(sessions, runtime)

    await apply(engine, profile="fast")
    assert (await counts(sessions, REFERENCES))[0] == 0
    assert (await counts(sessions, LARGE_INLINE_LINKS))[1] == 0
    found = await state(engine)
    assert found.lineage_triggers == {"mat_records": False, "mat_links": False}
    assert found.records_fillfactor == 90
    assert await snapshot(sessions, runtime) == before
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            [supplier("S-9", "Delta Springs", vat="IT9")],
            author=AUTHOR,
            message="inline maps",
            origin=ORIGIN,
            runtime=runtime,
        )
    assert (await counts(sessions, REFERENCES))[0] == 0

    await apply(engine, profile="compact")
    shared, total = await counts(sessions, REFERENCES)
    assert shared == total
    found = await state(engine)
    assert not found.values_index
    assert not found.lexicon_index
    assert found.lineage_triggers == {"mat_records": True, "mat_links": True}
    assert set(found.compression.values()) == {"pglz"}
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        indexed = {i.attribute_id for i in await existing_indexes(session, DOMAIN)}
    assert VAT in indexed
    assert RATING not in indexed
    assert (await snapshot(sessions, runtime)) == before

    # Every read still works without the indexes compact leaves out.
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        head = await head_schema(session, DOMAIN, "main")
        assert head is not None
        spec = index_spec(head.schema, SUPPLIER, RATING)
        assert spec is not None
        rated = await query_by_attribute(
            session, DOMAIN, "main", spec, Comparison.EQ, 7, limit=10, runtime=runtime
        )
        assert [r.key for r in rated] == ["S-2"]
        typos = TypoTolerance(threshold=0.3, weight=0.5, candidates=3, min_length=4)
        fixes = await search_corrections(session, DOMAIN, "main", "bearng", typos, runtime=runtime)
        assert [(c.word, c.replacements[0]) for c in fixes] == [("bearng", "bearing")]
        hood = await neighborhood(
            session, DOMAIN, "main", head.schema, [(PART, "P-1")], depth=2, runtime=runtime
        )
        assert hood.records
        assert all(n.record.lineage for n in hood.records)

    await apply(engine)
    found = await state(engine)
    assert found.values_index
    assert found.lexicon_index
    again = await storage.apply(engine, StoreSettings(), switch_wait_s=0)
    assert {step.status for step in again.steps} <= {"skipped"}, again.as_dict()


@pytest.mark.integration
async def test_overlay_reads_the_shared_maps_of_its_base(
    sessions: Sessions, runtime: StoreRuntime
) -> None:
    async with sessions() as session:
        await create_branch(
            session, DOMAIN, "agent/x", created_by="usr_test", mode="overlay", runtime=runtime
        )
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        base = await get_record(session, DOMAIN, "main", SUPPLIER, "S-1")
        overlay = await get_record(session, DOMAIN, "agent/x", SUPPLIER, "S-1")
    assert base is not None
    assert overlay is not None
    assert overlay.lineage == base.lineage
    assert overlay.lineage_revisions == base.lineage_revisions


@pytest.mark.integration
async def test_a_missing_map_is_an_error(sessions: Sessions) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        with pytest.raises(DBAPIError, match="lineage map"):
            await session.execute(
                text("SELECT lb_lineage(:d, CAST(:v AS jsonb))"),
                {"d": DOMAIN, "v": '{"$map": "00000000000000000000000000000000"}'},
            )


@pytest.mark.integration
async def test_maps_follow_row_level_security(engine: AsyncEngine, sessions: Sessions) -> None:
    """A role subject to RLS reads the maps of its Domain only, so a reference read in
    another Domain's transaction resolves to nothing it may see."""
    async with reader_role(engine, functions=False) as role:
        async with sessions() as session, session.begin():
            await session.execute(text(f"SET LOCAL ROLE {role}"))
            await scope_to_domain(session, DOMAIN)
            assert (await session.execute(MAPS, {"d": DOMAIN})).scalar_one() > 0
            record = await get_record(session, DOMAIN, "main", SUPPLIER, "S-1")
            assert record is not None
            assert record.lineage
        async with sessions() as session, session.begin():
            await session.execute(text(f"SET LOCAL ROLE {role}"))
            await scope_to_domain(session, "rival")
            assert (await session.execute(MAPS, {"d": DOMAIN})).scalar_one() == 0


FILLER = text(
    """
    INSERT INTO mat_records (domain_id, branch, kind_id, key, "values", derived, title, search,
                             object_hash, lineage, updated_revision, updated_at,
                             revision_generation, deleted, lineage_revisions)
    SELECT m.domain_id, m.branch, m.kind_id, 'F-' || lpad(g::text, 5, '0'), m."values", m.derived,
           m.title, m.search, m.object_hash, m.lineage, m.updated_revision, m.updated_at,
           m.revision_generation, m.deleted, m.lineage_revisions
    FROM mat_records m, generate_series(1, 4000) AS g
    WHERE m.domain_id = :d AND m.branch = 'main' AND m.kind_id = :k AND m.key = 'P-1'
    """
)
PAGES = text("SELECT pg_relation_size(CAST(:t AS regclass)) / 8192")
COUNT = text("SELECT count(*) FROM mat_records WHERE domain_id = :d")


@pytest.mark.integration
async def test_compaction_moves_rows_forward_and_gives_pages_back(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime
) -> None:
    """Rows at the end of a table with free space before them move there, VACUUM truncates
    the end, and every row reads the same."""
    async with sessions() as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        await session.execute(FILLER, {"d": DOMAIN, "k": PART})
        await session.execute(
            text("DELETE FROM mat_records WHERE domain_id = :d AND key < 'F-03000'"), {"d": DOMAIN}
        )
        partition = await session.scalar(
            text("SELECT tableoid::regclass::text FROM mat_records WHERE domain_id = :d LIMIT 1"),
            {"d": DOMAIN},
        )
    before = await snapshot(sessions, runtime)
    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await auto.exec_driver_sql(f"VACUUM {partition}")
        pages = (await auto.execute(PAGES, {"t": partition})).scalar_one()
        live = (await auto.execute(COUNT, {"d": DOMAIN})).scalar_one()
    report = storage.ApplyReport("balanced", {})
    await storage.compact(
        engine, StorageSettings(batch_rows=200), report, tables=("mat_records",), reindex=True
    )
    assert report.complete, report.as_dict()
    async with engine.connect() as connection:
        assert (await connection.execute(PAGES, {"t": partition})).scalar_one() < pages / 2
        assert (await connection.execute(COUNT, {"d": DOMAIN})).scalar_one() == live
    assert await snapshot(sessions, runtime) == before
    async with sessions() as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        await session.execute(
            text("DELETE FROM mat_records WHERE domain_id = :d AND key LIKE 'F-%'"), {"d": DOMAIN}
        )


@pytest.mark.integration
async def test_downgrade_restores_maps_and_an_upgraded_install_starts_inline(
    engine: AsyncEngine,
    sessions: Sessions,
    runtime: StoreRuntime,
    database_url: str,
    database: DatabaseSettings,
    store: StoreSettings,
) -> None:
    """The downgrade writes every map back into its rows; upgrading a database that holds
    Records leaves the triggers off (processes of the previous version may still run) until
    `storage apply`."""
    before = await snapshot(sessions, runtime)
    await engine.dispose()
    await asyncio.to_thread(
        migrations.downgrade, "0035", database=database, store=store, url=database_url
    )
    async with engine.connect() as connection:
        await connection.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', false)"))
        held = await connection.scalar(
            text("SELECT count(*) FROM mat_records WHERE lineage ? '$map'")
        )
    assert held == 0
    await engine.dispose()
    await asyncio.to_thread(migrations.upgrade, database=database, store=store, url=database_url)
    found = await state(engine)
    assert found.lineage_triggers == {"mat_records": False, "mat_links": False}
    assert (await counts(sessions, REFERENCES))[0] == 0
    assert await snapshot(sessions, runtime) == before
    await apply(engine)
    shared, total = await counts(sessions, REFERENCES)
    assert shared == total
    assert await snapshot(sessions, runtime) == before
