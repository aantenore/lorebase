"""Read primitives on materialized heads (lorebase_store.reads) and their query plans."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import Domain, Schema
from lorebase_store.commit import SchemaUpsert, commit, create_domain
from lorebase_store.materialize import create_branch, verify
from lorebase_store.reads import (
    Comparison,
    Direction,
    get_record,
    head_revision,
    head_schema,
    hydrate_records,
    index_spec,
    links_of_record,
    list_records,
    query_by_attribute,
    search_records,
)
from lorebase_store.repositories import scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.schema_cache import SchemaCache
from lorebase_store.traverse import neighborhood

from .read_support import (
    SOURCED_FROM,
    captured,
    plan_of,
    rated_supplier,
    scans,
    seed_graph,
)
from .support import AUTHOR, DOMAIN, ORIGIN, PART, RATING, SUPPLIED_BY, SUPPLIER, VAT, Clock, domain

Sessions = async_sessionmaker[AsyncSession]


def test_schema_cache_is_a_bounded_lru() -> None:
    cache = SchemaCache(2)
    schema = Schema(domain=Domain(id="d"))
    cache.put("d", b"1", schema)
    cache.put("d", b"2", schema)
    assert cache.get("d", b"1") is schema
    cache.put("d", b"3", schema)
    assert cache.get("d", b"2") is None
    assert cache.get("d", b"1") is schema
    assert (cache.hits, cache.misses, len(cache)) == (2, 1, 2)
    disabled = SchemaCache(0)
    disabled.put("d", b"1", schema)
    assert len(disabled) == 0


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
    assert report.ok, report.divergences


@pytest.mark.integration
async def test_head_and_schema_cache(sessions: Sessions, runtime: StoreRuntime) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        head = await head_revision(session, DOMAIN, "main")
        first = await head_schema(session, DOMAIN, "main", runtime=runtime)
        second = await head_schema(session, DOMAIN, "main", runtime=runtime)
        assert await head_revision(session, DOMAIN, "nope") is None
        assert await head_schema(session, DOMAIN, "nope", runtime=runtime) is None
    assert first is not None
    assert second is not None
    assert first.revision == head
    assert second.schema is first.schema
    assert (runtime.schema_cache.hits, runtime.schema_cache.misses) == (1, 1)

    kind = rated_supplier()
    attributes = dict(kind.attributes)
    attributes[VAT] = attributes[VAT].model_copy(update={"description": "Tax id."})
    async with sessions() as session:
        moved = await commit(
            session,
            DOMAIN,
            "main",
            [],
            schema_changes=[SchemaUpsert(kind.model_copy(update={"attributes": attributes}))],
            author=AUTHOR,
            message="describe",
            origin=ORIGIN,
            runtime=runtime,
        )
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        third = await head_schema(session, DOMAIN, "main", runtime=runtime)
    assert third is not None
    assert third.revision == moved.revision_id != head
    assert third.schema.kinds[SUPPLIER].attributes[VAT].description == "Tax id."


@pytest.mark.integration
async def test_records_links_queries_and_search(sessions: Sessions, runtime: StoreRuntime) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = await head_schema(session, DOMAIN, "main", runtime=runtime)
        assert found is not None
        schema = found.schema

        record = await get_record(session, DOMAIN, "main", SUPPLIER, "S-2")
        assert record is not None
        assert record.title == "Beta Seals"
        assert await get_record(session, DOMAIN, "main", SUPPLIER, "S-9") is None

        page = await list_records(session, DOMAIN, "main", PART, limit=2, runtime=runtime)
        rest = await list_records(
            session, DOMAIN, "main", PART, limit=10, after=page[-1].key, runtime=runtime
        )
        assert [r.key for r in page + rest] == ["P-1", "P-2", "P-3", "P-4"]

        hydrated = await hydrate_records(
            session, DOMAIN, "main", [(PART, "P-4"), (SUPPLIER, "S-1"), (PART, "P-9")]
        )
        assert set(hydrated) == {(PART, "P-4"), (SUPPLIER, "S-1")}

        incoming = await links_of_record(
            session, DOMAIN, "main", schema, SUPPLIER, "S-2", limit=10, runtime=runtime
        )
        assert [(link.relation_id, link.from_key) for link in incoming] == [
            (SUPPLIED_BY, "P-4"),
            (SOURCED_FROM, "P-1"),
        ]
        assert incoming[0].object_hash is not None
        assert incoming[1].object_hash is None
        outgoing = await links_of_record(
            session,
            DOMAIN,
            "main",
            schema,
            PART,
            "P-1",
            direction=Direction.OUT,
            limit=10,
            runtime=runtime,
        )
        assert [(link.relation_id, link.to_key) for link in outgoing] == [
            (SUPPLIED_BY, "S-1"),
            (SOURCED_FROM, "S-2"),
        ]
        only = await links_of_record(
            session,
            DOMAIN,
            "main",
            schema,
            PART,
            "P-1",
            relation_ids=[SOURCED_FROM],
            limit=10,
            runtime=runtime,
        )
        assert [link.relation_id for link in only] == [SOURCED_FROM]

        rating = index_spec(schema, SUPPLIER, RATING)
        assert rating is not None
        above = await query_by_attribute(
            session, DOMAIN, "main", rating, Comparison.GT, "5", limit=1, runtime=runtime
        )
        assert [r.key for r in above] == ["S-2"]
        after = (above[0].values[RATING], above[0].key)
        more = await query_by_attribute(
            session,
            DOMAIN,
            "main",
            rating,
            Comparison.GT,
            "5",
            limit=5,
            after=after,
            runtime=runtime,
        )
        assert [r.key for r in more] == ["S-3"]
        vat = index_spec(schema, SUPPLIER, VAT)
        assert vat is not None
        exact = await query_by_attribute(
            session, DOMAIN, "main", vat, Comparison.EQ, "IT3", limit=5, runtime=runtime
        )
        assert [r.key for r in exact] == ["S-3"]

        hits = await search_records(session, DOMAIN, "main", "bearing", limit=5, runtime=runtime)
        assert {(h.kind_id, h.key) for h in hits} == {(PART, "P-1"), (PART, "P-3")}
        assert all(h.score > 0 for h in hits)
        suppliers = await search_records(
            session, DOMAIN, "main", "bearings", limit=5, kind_ids=[SUPPLIER], runtime=runtime
        )
        assert [h.key for h in suppliers] == ["S-1"]


# Plans at scale ---------------------------------------------------------------------------------

BULK_ROWS = 20000
BULK_BRANCH = "bulk"

_BULK_RECORDS = text(
    """
    INSERT INTO mat_records (domain_id, branch, kind_id, key, "values", title, search,
                             object_hash, lineage, updated_revision, updated_at)
    SELECT CAST(:domain AS text), CAST(:branch AS text), kind, prefix || g,
           CASE WHEN kind = :supplier
                THEN jsonb_build_object(CAST(:vat AS text), 'VAT' || g,
                                        CAST(:rating AS text), CAST(g % 100 AS text))
                ELSE '{}'::jsonb END,
           title, to_tsvector('simple', title), sha256(convert_to(prefix || g, 'UTF8')), '{}',
           :revision, now()
    FROM generate_series(1, :rows) AS g,
         LATERAL (VALUES (CAST(:supplier AS text), 'BS-',
                          'Supplier ' || g || ' metal' || (g % 500)),
                         (CAST(:part AS text), 'BP-', 'Part ' || g || ' alloy' || (g % 700)))
           AS k(kind, prefix, title)
    """
)
_BULK_LINKS = text(
    """
    INSERT INTO mat_links (domain_id, branch, relation_id, from_key, to_key, "values",
                           object_hash, lineage)
    SELECT :domain, :branch, :relation, 'BP-' || g, 'BS-' || ((g * 7) % :rows + 1), '{}',
           sha256(convert_to('BP-' || g, 'UTF8')), '{}'
    FROM generate_series(1, :rows) AS g
    """
)


@pytest.fixture
async def bulk(engine: AsyncEngine, runtime: StoreRuntime) -> Sessions:
    """A `bulk` branch with `BULK_ROWS` extra Suppliers and Parts written directly as rows."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=Clock())
    async with factory() as session:
        seeded = await seed_graph(session, runtime)
    async with factory() as session:
        await create_branch(session, DOMAIN, BULK_BRANCH, created_by="usr_test", runtime=runtime)
    async with factory() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        params = {"domain": DOMAIN, "branch": BULK_BRANCH, "rows": BULK_ROWS}
        await session.execute(
            _BULK_RECORDS,
            {
                **params,
                "supplier": SUPPLIER,
                "part": PART,
                "vat": VAT,
                "rating": RATING,
                "revision": seeded.revision_id,
            },
        )
        await session.execute(_BULK_LINKS, {**params, "relation": SUPPLIED_BY})
    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await auto.exec_driver_sql("VACUUM ANALYZE mat_records, mat_links")
    return factory


async def _exercise(session: AsyncSession, schema: Schema, runtime: StoreRuntime) -> None:
    """One call of every hot read primitive on the bulk branch."""
    await get_record(session, DOMAIN, BULK_BRANCH, SUPPLIER, "BS-77")
    await list_records(session, DOMAIN, BULK_BRANCH, PART, limit=50, after="BP-5", runtime=runtime)
    await links_of_record(
        session, DOMAIN, BULK_BRANCH, schema, SUPPLIER, "BS-8", limit=50, runtime=runtime
    )
    await neighborhood(session, DOMAIN, BULK_BRANCH, schema, [(PART, "BP-11")], runtime=runtime)
    await hydrate_records(session, DOMAIN, BULK_BRANCH, [(PART, "BP-1"), (SUPPLIER, "BS-2")])


def _assert_index_plans(plans: list[list[tuple[str, str | None]]]) -> None:
    for plan in plans:
        assert plan, "every statement reads a table"
        assert all(node != "Seq Scan" for node, _ in plan), plan


@pytest.mark.integration
async def test_hot_paths_use_indexes_at_scale(
    engine: AsyncEngine, bulk: Sessions, runtime: StoreRuntime
) -> None:
    async with bulk() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = await head_schema(session, DOMAIN, BULK_BRANCH, runtime=runtime)
        assert found is not None
        schema = found.schema
        with captured(engine) as statements:
            await _exercise(session, schema, runtime)
            rating = index_spec(schema, SUPPLIER, RATING)
            vat = index_spec(schema, SUPPLIER, VAT)
            assert rating is not None
            assert vat is not None
            await query_by_attribute(
                session, DOMAIN, BULK_BRANCH, rating, Comparison.EQ, "42", limit=50
            )
            await query_by_attribute(
                session, DOMAIN, BULK_BRANCH, vat, Comparison.EQ, "VAT99", limit=50
            )
            await search_records(session, DOMAIN, BULK_BRANCH, "metal42", limit=20)
        reads = [(sql, params) for sql, params in statements if "set_config" not in sql]
        connection = await session.connection()
        plans = [scans(await plan_of(connection, sql, params)) for sql, params in reads]
        generic = [
            scans(await plan_of(connection, sql, params, generic=True)) for sql, params in reads
        ]
    _assert_index_plans(plans)
    _assert_index_plans(generic)
    used = {name for plan in plans for _, name in plan if name}
    assert any(name.startswith("mat_records_attr_") for name in used)
    assert any(name.endswith("search_idx") for name in used), used
    assert any("pkey" in name for name in used)
    assert any("to_key" in name for name in used), "incoming Links use the reverse index"


@pytest.mark.integration
async def test_key_and_link_paths_keep_index_plans_under_row_level_security(
    engine: AsyncEngine, bulk: Sessions, runtime: StoreRuntime
) -> None:
    """Under RLS only leakproof operators become index conditions (text equality and order
    are). Key, page and stored-Link conditions stay index conditions; inbound foreign-key
    Links still read through their partial index, but as a filter (design 8.7)."""
    role = f"lorebase_reader_{uuid.uuid4().hex[:8]}"
    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await auto.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
        await auto.exec_driver_sql(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}")
    try:
        async with bulk() as session, session.begin():
            await session.execute(text(f"SET LOCAL ROLE {role}"))
            await scope_to_domain(session, DOMAIN)
            found = await head_schema(session, DOMAIN, BULK_BRANCH, runtime=runtime)
            assert found is not None
            with captured(engine) as statements:
                await _exercise(session, found.schema, runtime)
            reads = [(sql, params) for sql, params in statements if "set_config" not in sql]
            connection = await session.connection()
            plans = [scans(await plan_of(connection, sql, params)) for sql, params in reads]
            record = await get_record(session, DOMAIN, BULK_BRANCH, SUPPLIER, "BS-77")
        assert record is not None
        _assert_index_plans(plans)
    finally:
        async with engine.connect() as connection:
            auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await auto.exec_driver_sql(f"DROP OWNED BY {role}")
            await auto.exec_driver_sql(f"DROP ROLE {role}")
