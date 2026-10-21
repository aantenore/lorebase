"""Read primitives on materialized heads (lorebase_store.reads) and their query plans."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import Domain, Schema
from lorebase_store.commit import SchemaUpsert, commit, create_domain
from lorebase_store.definer import READ_FUNCTIONS, grant_read_functions
from lorebase_store.layout import BranchLayout, load_layout
from lorebase_store.materialize import create_branch, verify
from lorebase_store.reads import (
    Comparison,
    Direction,
    TitleSimilarity,
    get_record,
    head_revision,
    head_schema,
    hydrate_records,
    index_spec,
    links_of_record,
    links_to_record,
    list_records,
    query_by_attribute,
    search_records,
)
from lorebase_store.repositories import scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.schema_cache import SchemaCache
from lorebase_store.settings import ReadSettings, StoreSettings
from lorebase_store.traverse import neighborhood

from .read_support import (
    SOURCED_FROM,
    captured,
    nested_plans,
    plan_nodes,
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


def reads_runtime(*, definer: bool) -> StoreRuntime:
    """Store runtime whose reads use the security definer functions or plain SQL."""
    reads = ReadSettings(use_definer_functions=definer)
    return StoreRuntime.from_settings(StoreSettings(reads=reads))


@pytest.mark.integration
@pytest.mark.parametrize("definer", [True, False], ids=["definer", "plain"])
async def test_records_links_queries_and_search(sessions: Sessions, definer: bool) -> None:
    """Both paths, the security definer functions and plain SQL, answer the same."""
    runtime = reads_runtime(definer=definer)
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
        assert await search_records(session, DOMAIN, "main", "bearing", limit=5, kind_ids=[]) == []
        typo = await search_records(
            session,
            DOMAIN,
            "main",
            "Gama Filtres",
            limit=5,
            titles=TitleSimilarity(threshold=0.3, weight=0.5),
            runtime=runtime,
        )
        assert [h.key for h in typo] == ["S-3"]
        assert typo[0].score > 0
        assert await search_records(session, DOMAIN, "main", "Gama Filtres", limit=5) == []

        supplied = schema.relations[SUPPLIED_BY]
        first = await links_to_record(
            session, DOMAIN, "main", supplied, "S-1", limit=2, runtime=runtime
        )
        assert [link.from_key for link in first] == ["P-1", "P-2"]
        assert first[0].object_hash is not None
        rest = await links_to_record(
            session, DOMAIN, "main", supplied, "S-1", limit=2, after="P-2", runtime=runtime
        )
        assert [link.from_key for link in rest] == ["P-3"]
        sourced = schema.relations[SOURCED_FROM]
        foreign = await links_to_record(
            session, DOMAIN, "main", sourced, "S-2", limit=5, runtime=runtime
        )
        assert [(link.relation_id, link.from_key, link.to_key) for link in foreign] == [
            (SOURCED_FROM, "P-1", "S-2")
        ]
        assert foreign[0].values == {}
        assert foreign[0].object_hash is None
        assert (
            await links_to_record(
                session, DOMAIN, "main", sourced, "S-2", limit=5, after="P-1", runtime=runtime
            )
            == []
        )


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


async def _exercise(
    session: AsyncSession,
    schema: Schema,
    runtime: StoreRuntime,
    branch: str | BranchLayout = BULK_BRANCH,
) -> None:
    """One call of every hot read primitive on the bulk branch (or `branch`)."""
    await get_record(session, DOMAIN, branch, SUPPLIER, "BS-77")
    await list_records(session, DOMAIN, branch, PART, limit=50, after="BP-5", runtime=runtime)
    await links_of_record(
        session, DOMAIN, branch, schema, SUPPLIER, "BS-8", limit=50, runtime=runtime
    )
    await links_to_record(
        session,
        DOMAIN,
        branch,
        schema.relations[SOURCED_FROM],
        "BS-8",
        limit=50,
        runtime=runtime,
    )
    await neighborhood(session, DOMAIN, branch, schema, [(PART, "BP-11")], runtime=runtime)
    await hydrate_records(session, DOMAIN, branch, [(PART, "BP-1"), (SUPPLIER, "BS-2")])
    rating = index_spec(schema, SUPPLIER, RATING)
    vat = index_spec(schema, SUPPLIER, VAT)
    assert rating is not None
    assert vat is not None
    for spec, comparison, value in (
        (rating, Comparison.EQ, "42"),
        (rating, Comparison.GTE, "98"),
        (vat, Comparison.EQ, "VAT99"),
    ):
        await query_by_attribute(
            session, DOMAIN, branch, spec, comparison, value, limit=50, runtime=runtime
        )
    await search_records(session, DOMAIN, branch, "metal42", limit=20, runtime=runtime)
    await search_records(
        session,
        DOMAIN,
        branch,
        "Suplier 77",
        limit=20,
        titles=TitleSimilarity(threshold=0.6, weight=0.5),
        runtime=runtime,
    )


def _assert_index_plans(plans: list[list[tuple[str, str | None]]]) -> None:
    for plan in plans:
        assert plan, "every statement reads a table"
        assert all(node != "Seq Scan" for node, _ in plan), plan


def _assert_index_kinds(plans: list[list[tuple[str, str | None]]]) -> None:
    used = {name for plan in plans for _, name in plan if name}
    assert any(name.startswith("mat_records_attr_") for name in used), used
    assert any(name.endswith("search_idx") for name in used), used
    assert any(name.endswith("title_idx") for name in used), used
    assert any("pkey" in name for name in used), used
    assert any("to_key" in name for name in used), "incoming Links use the reverse index"


@asynccontextmanager
async def reader_role(engine: AsyncEngine, *, functions: bool) -> AsyncIterator[str]:
    """A role subject to row-level security, like the application role, with SELECT on the
    tables and, when `functions`, EXECUTE on the read functions."""
    role = f"lorebase_reader_{uuid.uuid4().hex[:8]}"
    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await auto.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
        await auto.exec_driver_sql(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}")
        if functions:
            assert len(await grant_read_functions(auto, role)) == len(READ_FUNCTIONS)
    try:
        yield role
    finally:
        async with engine.connect() as connection:
            auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await auto.exec_driver_sql(f"DROP OWNED BY {role}")
            await auto.exec_driver_sql(f"DROP ROLE {role}")


@pytest.mark.integration
async def test_plain_paths_use_indexes_at_scale(engine: AsyncEngine, bulk: Sessions) -> None:
    """Plain SQL (`use_definer_functions` off) for a role not subject to RLS: every statement,
    with its parameters and as the generic plan of a prepared statement, is an index scan."""
    runtime = reads_runtime(definer=False)
    async with bulk() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = await head_schema(session, DOMAIN, BULK_BRANCH, runtime=runtime)
        assert found is not None
        layout = await load_layout(session, DOMAIN, BULK_BRANCH)
        with captured(engine) as statements:
            await _exercise(session, found.schema, runtime, layout)
        reads = [(sql, params) for sql, params in statements if "set_config" not in sql]
        connection = await session.connection()
        plans = [scans(await plan_of(connection, sql, params)) for sql, params in reads]
        generic = [
            scans(await plan_of(connection, sql, params, generic=True)) for sql, params in reads
        ]
    _assert_index_plans(plans)
    _assert_index_plans(generic)
    _assert_index_kinds(plans)


@pytest.mark.integration
async def test_definer_functions_keep_index_plans_under_row_level_security(
    engine: AsyncEngine, bulk: Sessions
) -> None:
    """Under RLS only leakproof operators (text equality and order) can be index conditions
    of plain SQL. Through the security definer functions every read, including incoming
    foreign-key Links, attribute comparisons and search, is an index scan (design 8.7): the
    plans checked are the ones run inside the functions, as `auto_explain` reports them."""
    runtime = reads_runtime(definer=True)
    async with reader_role(engine, functions=True) as role:
        async with bulk() as session, session.begin():
            async with nested_plans(session) as plans:
                await session.execute(text(f"SET LOCAL ROLE {role}"))
                await scope_to_domain(session, DOMAIN)
                found = await head_schema(session, DOMAIN, BULK_BRANCH, runtime=runtime)
                assert found is not None
                layout = await load_layout(session, DOMAIN, BULK_BRANCH)
                await _exercise(session, found.schema, runtime, layout)
            record = await get_record(session, DOMAIN, BULK_BRANCH, SUPPLIER, "BS-77")
        assert record is not None
    reading = [found for found in (scans(plan) for plan in plans) if found]
    functions = [
        node["Function Name"]
        for plan in plans
        for node in plan_nodes(plan)
        if node.get("Function Name", "").startswith("lb_")
    ]
    assert set(functions) == {f for f in READ_FUNCTIONS if not f.endswith("_overlay")}
    _assert_index_plans(reading)
    _assert_index_kinds(reading)


OVERLAY_BRANCH = "agent/bulk"

_OVERLAY_ROWS = text(
    """
    INSERT INTO mat_records (domain_id, branch, kind_id, key, "values", derived, title, search,
                             object_hash, lineage, updated_revision, updated_at,
                             revision_generation, deleted)
    SELECT domain_id, :overlay, kind_id, key, "values", derived, title || ' changed',
           to_tsvector('simple', title || ' changed'), object_hash, lineage, updated_revision,
           updated_at, revision_generation, kind_id = :part
    FROM mat_records
    WHERE domain_id = :domain AND branch = :bulk
      AND key = ANY(SELECT p || g FROM generate_series(1, 500) AS g, (VALUES ('BS-'), ('BP-')) v(p))
    """
)


async def _bulk_overlay(engine: AsyncEngine, bulk: Sessions, runtime: StoreRuntime) -> None:
    """An overlay of the bulk branch holding 500 changed Suppliers and 500 Part tombstones."""
    async with bulk() as session:
        await create_branch(
            session,
            DOMAIN,
            OVERLAY_BRANCH,
            created_by="usr_test",
            source=BULK_BRANCH,
            mode="overlay",
            runtime=runtime,
        )
    async with bulk() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await session.execute(
            _OVERLAY_ROWS,
            {"overlay": OVERLAY_BRANCH, "domain": DOMAIN, "bulk": BULK_BRANCH, "part": PART},
        )
    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await auto.exec_driver_sql("VACUUM ANALYZE mat_records, mat_links, refs")


def _mat_scans(plan: dict[str, Any]) -> list[tuple[str, str]]:
    """`(node type, relation or index)` of the scans of materialized tables."""
    return [
        (node["Node Type"], node.get("Index Name") or node.get("Relation Name", ""))
        for node in plan_nodes(plan)
        if str(node.get("Relation Name") or node.get("Index Name") or "").startswith("mat_")
    ]


def _assert_overlay_plans(plans: list[dict[str, Any]]) -> None:
    """Every statement reads both the overlay and its base, through indexes only."""
    reading = [found for found in (_mat_scans(plan) for plan in plans) if found]
    assert reading
    for found in reading:
        assert all(node != "Seq Scan" for node, _ in found), found
    used = {name for found in reading for _, name in found}
    assert any(name.startswith("mat_records_attr_") for name in used), used
    assert any(name.endswith("search_idx") for name in used), used
    assert any("pkey" in name for name in used), used


@pytest.mark.integration
async def test_overlay_reads_keep_index_plans(
    engine: AsyncEngine, bulk: Sessions, runtime: StoreRuntime
) -> None:
    """On an overlay the statements read the resolved rows (overlay over base): plain SQL
    for a role not subject to RLS, custom and generic plans, and the `_overlay` definer
    functions for a role subject to it, all on the indexes of the base and the overlay."""
    await _bulk_overlay(engine, bulk, runtime)
    plain = reads_runtime(definer=False)
    async with bulk() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = await head_schema(session, DOMAIN, OVERLAY_BRANCH, runtime=plain)
        assert found is not None
        layout = await load_layout(session, DOMAIN, OVERLAY_BRANCH)
        assert layout.overlay
        with captured(engine) as statements:
            await _exercise(session, found.schema, plain, layout)
        reads = [(sql, params) for sql, params in statements if "set_config" not in sql]
        connection = await session.connection()
        plans = [await plan_of(connection, sql, params) for sql, params in reads]
        generic = [await plan_of(connection, sql, params, generic=True) for sql, params in reads]
    _assert_overlay_plans(plans)
    _assert_overlay_plans(generic)

    definer = reads_runtime(definer=True)
    async with reader_role(engine, functions=True) as role, bulk() as session, session.begin():
        async with nested_plans(session) as nested:
            await session.execute(text(f"SET LOCAL ROLE {role}"))
            await scope_to_domain(session, DOMAIN)
            await _exercise(session, found.schema, definer, layout)
        changed = await get_record(session, DOMAIN, layout, SUPPLIER, "BS-77")
        gone = await get_record(session, DOMAIN, layout, PART, "BP-77")
        kept = await get_record(session, DOMAIN, layout, PART, "BP-777")
    functions = {
        node["Function Name"]
        for plan in nested
        for node in plan_nodes(plan)
        if node.get("Function Name", "").startswith("lb_")
    }
    assert functions == {f for f in READ_FUNCTIONS if f.endswith("_overlay")}
    _assert_overlay_plans(nested)
    assert changed is not None
    assert changed.title is not None
    assert changed.title.endswith(" changed")
    assert gone is None
    assert kept is not None


RIVAL = "rival"


@pytest.mark.integration
async def test_read_functions_read_only_the_domain_they_are_given(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime
) -> None:
    """The functions pin the Domain argument in every predicate and never read the
    transaction's RLS scope; EXECUTE is revoked from PUBLIC."""
    async with sessions() as session:
        rival = Domain(id=RIVAL, display_name="Rival", description="Another Domain.")
        await create_domain(session, rival, author=AUTHOR, runtime=runtime, clock=Clock())
    async with sessions() as session:
        await seed_graph(session, runtime, domain_id=RIVAL, label="Rival ")
    definer, plain = reads_runtime(definer=True), reads_runtime(definer=False)
    async with reader_role(engine, functions=False) as role, sessions() as session:
        await session.execute(text(f"SET LOCAL ROLE {role}"))
        await scope_to_domain(session, DOMAIN)
        with pytest.raises(DBAPIError, match="permission denied"):
            await search_records(session, DOMAIN, "main", "bearing", limit=5, runtime=definer)
        await session.rollback()
    async with (
        reader_role(engine, functions=True) as role,
        sessions() as session,
        session.begin(),
    ):
        await session.execute(text(f"SET LOCAL ROLE {role}"))
        await scope_to_domain(session, RIVAL)
        found = await head_schema(session, RIVAL, "main", runtime=runtime)
        assert found is not None
        schema = found.schema
        titles: list[str | None] = []
        for domain_id in (DOMAIN, RIVAL):
            hits = await search_records(
                session, domain_id, "main", "bearing", limit=10, runtime=definer
            )
            assert {h.key for h in hits} == {"P-1", "P-3"}
            titles += [h.title for h in hits if domain_id == DOMAIN]
            assert all((h.title or "").startswith("Rival") == (domain_id == RIVAL) for h in hits)
        rating = index_spec(schema, SUPPLIER, RATING)
        assert rating is not None
        rated = await query_by_attribute(
            session, DOMAIN, "main", rating, Comparison.GT, "5", limit=5, runtime=definer
        )
        assert [r.key for r in rated] == ["S-2", "S-3"]
        titles += [r.title for r in rated]
        sourced = schema.relations[SOURCED_FROM]
        linked = await links_to_record(
            session, DOMAIN, "main", sourced, "S-2", limit=5, runtime=definer
        )
        assert [link.from_key for link in linked] == ["P-1"]
        hood = await neighborhood(
            session, DOMAIN, "main", schema, [(PART, "P-1")], depth=1, runtime=definer
        )
        assert [n.record.key for n in hood.records] == ["P-1", "S-1", "S-2"]
        titles += [n.record.title for n in hood.records]
        assert titles
        assert not any((title or "").startswith("Rival") for title in titles)
        # Plain SQL stays inside the transaction's RLS scope.
        assert (
            await search_records(session, DOMAIN, "main", "bearing", limit=10, runtime=plain) == []
        )
