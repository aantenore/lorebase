"""BM25 search through ParadeDB `pg_search` (revision 0037, `lorebase_store.bm25`).

The tests that reach the BM25 index run where the server offers and preloads `pg_search` (a
ParadeDB image: `LOREBASE_TEST_POSTGRES_IMAGE`, or `LOREBASE_TEST_DATABASE_URL` on such a
server) and are skipped elsewhere. The checks of the definer functions, which refuse a call
before any index is read, and the fallback without the extension run everywhere.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.validate import RecordDelete
from lorebase_store import bm25, storage
from lorebase_store.commit import commit, create_domain
from lorebase_store.materialize import BranchMode, create_branch
from lorebase_store.reads import (
    PartialMatching,
    SearchHits,
    TypoTolerance,
    search_corrections,
    search_records,
)
from lorebase_store.repositories import scope_to_domain
from lorebase_store.repositories.refs import sqlstate
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import StorageSettings, StoreSettings

from .read_support import part_with, seed_graph
from .support import AUTHOR, DOMAIN, ORIGIN, PART, SUPPLIER, Clock, domain, supplier
from .test_reads import reader_role

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]

OVERLAY = "agent/bm25"
TYPOS = TypoTolerance(threshold=0.3, weight=0.5, candidates=3, min_length=3)
PARTIAL = PartialMatching(min_results=1, weight=0.5, term_bonus=1.0, candidates=20)
QUERIES = (
    "bearing",
    "bearings",
    "seal -oil",
    "washer or seal",
    '"roller bearing"',
    '"bearing roller"',
    "stock -9",
    "stock 3 or stock 5",
    "-stock",
    "gamma filters",
    "nothinglikethis",
)
"""Rare terms of the seeded graph: every match is ranked by both backends."""


async def _pg_search(engine: AsyncEngine) -> bool:
    async with engine.connect() as connection:
        available = await connection.scalar(
            text("SELECT 1 FROM pg_available_extensions WHERE name = 'pg_search'")
        )
        preload = str(await connection.scalar(text("SHOW shared_preload_libraries")))
    return bool(available) and "pg_search" in preload


async def _seed(engine: AsyncEngine, runtime: StoreRuntime) -> Sessions:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=Clock())
    async with factory() as session:
        await seed_graph(session, runtime)
    await _put(factory, runtime, "main", [part_with(f"SP-{n}", f"Stock {n}") for n in range(12)])
    return factory


async def _put(sessions: Sessions, runtime: StoreRuntime, branch: str, changes: Any) -> None:
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            branch,
            list(changes),
            author=AUTHOR,
            message=f"change {branch}",
            origin=ORIGIN,
            runtime=runtime,
        )


async def _apply(engine: AsyncEngine, wanted: bool) -> storage.ApplyReport:
    settings = StoreSettings(storage=StorageSettings(bm25_index=wanted))
    report = await storage.apply(engine, settings, switch_wait_s=0)
    bm25.forget()
    return report


@pytest.fixture
async def indexed(engine: AsyncEngine, runtime: StoreRuntime) -> AsyncIterator[Sessions]:
    """The small graph plus twelve Stock Parts, with the BM25 index built by `storage
    apply`."""
    if not await _pg_search(engine):
        pytest.skip("the server does not offer and preload pg_search (a ParadeDB image)")
    sessions = await _seed(engine, runtime)
    report = await _apply(engine, True)
    assert [s.status for s in report.steps if s.step == "bm25 index"] == ["done"]
    yield sessions
    bm25.forget()


def _hits(hits: SearchHits) -> list[tuple[str, str, bool]]:
    return sorted((h.kind_id, h.key, h.exact) for h in hits)


async def _both(
    session: AsyncSession, branch: str, query: str, **options: Any
) -> tuple[SearchHits, SearchHits]:
    default = await search_records(session, DOMAIN, branch, query, limit=50, **options)
    ranked = await search_records(
        session, DOMAIN, branch, query, limit=50, backend="bm25", **options
    )
    return default, ranked


async def _assert_parity(session: AsyncSession, branch: str) -> None:
    """Every query finds the same Records with both backends; BM25 ranks them all."""
    for query in QUERIES:
        for kinds in (None, [PART], [SUPPLIER]):
            default, ranked = await _both(session, branch, query, kind_ids=kinds)
            assert ranked.backend == "bm25", query
            assert _hits(ranked) == _hits(default), (branch, query, kinds)
            assert not ranked.truncated, query
            scores = [h.score for h in ranked]
            assert scores == sorted(scores, reverse=True), query
    for query in ("Gama Filtres", "roler bearing", "Stok 4"):
        fixes = await search_corrections(session, DOMAIN, branch, query, TYPOS)
        assert fixes or branch == "main", query  # main renames the only roller bearing
        default, ranked = await _both(session, branch, query, corrections=fixes)
        assert _hits(ranked) == _hits(default), (branch, query)
        exact = [h.exact for h in ranked]
        assert exact == sorted(exact, reverse=True), query
    for query in ("bearing nothinglikethis", "washer seal -flat"):
        default, ranked = await _both(session, branch, query, partial=PARTIAL)
        assert sorted((h.key, h.partial) for h in ranked) == sorted(
            (h.key, h.partial) for h in default
        ), (branch, query)


async def test_bm25_finds_what_full_text_search_finds(
    indexed: Sessions, runtime: StoreRuntime
) -> None:
    async with indexed() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await _assert_parity(session, "main")
        assert await search_records(session, DOMAIN, "main", "bearing", limit=5, kind_ids=[]) == []
        assert await search_records(session, DOMAIN, "main", "the", limit=5, backend="bm25") == []
        top = await search_records(session, DOMAIN, "main", "stock", limit=3, backend="bm25")
        assert len(top) == 3
        assert not top.truncated
        assert top[0].score == 1.0  # relative to the best hit
        assert all(0 < h.score <= 1 for h in top)

    # Lexemes are indexed as the vector holds them, case included; a row without a vector
    # matches no query, a negation-only one neither.
    async with indexed() as session, session.begin():
        await session.execute(
            text(
                "UPDATE mat_records SET search = CASE key WHEN 'SP-3' THEN "
                "CAST('Ärger:1 zeta:2' AS tsvector) END "
                "WHERE domain_id = :d AND branch = 'main' AND key IN ('SP-3', 'SP-4')"
            ),
            {"d": DOMAIN},
        )
    async with indexed() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        upper = json.dumps({"term": {"field": "terms", "value": "Ärger"}})
        found = await session.execute(
            text(
                "SELECT key FROM lb_search_bm25(:d, 'main', NULL, 'simple', 'zeta', 10, 10, "
                "CAST(:m AS jsonb), true)"
            ),
            {"d": DOMAIN, "m": upper},
        )
        assert list(found.scalars()) == ["SP-3"]
        for query in ("-stock", "-bearing -seal"):
            default, ranked = await _both(session, "main", query)
            assert ranked.backend == "bm25"
            assert _hits(ranked) == _hits(default), query
            assert "SP-4" not in {h.key for h in ranked}

    # Commits reach the index in the same transaction: new, changed and deleted rows.
    await _put(
        indexed,
        runtime,
        "main",
        [
            part_with("P-9", "Quixotic sprocket"),
            part_with("P-2", "Grooved washer"),
            part_with("SP-0", "Renamed item"),
        ],
    )
    async with indexed() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = await search_records(session, DOMAIN, "main", "quixotic", limit=5, backend="bm25")
        assert found.backend == "bm25"
        assert [h.key for h in found] == ["P-9"]
        flat = await search_records(session, DOMAIN, "main", "flat", limit=5, backend="bm25")
        assert flat == []
        await _assert_parity(session, "main")


async def test_bm25_on_an_overlay_reads_its_rows_over_the_base(
    indexed: Sessions, runtime: StoreRuntime
) -> None:
    async with indexed() as session:
        created = await create_branch(
            session,
            DOMAIN,
            OVERLAY,
            created_by="usr_test",
            mode=BranchMode.OVERLAY,
            runtime=runtime,
        )
    assert created.mode.value == "overlay"
    async with indexed() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await _assert_parity(session, OVERLAY)
    # The overlay changes, adds and deletes rows; the base then changes rows the overlay
    # reads (copied into it) and adds rows it must not see.
    await _put(
        indexed,
        runtime,
        OVERLAY,
        [
            part_with("P-3", "Ball bearing, sealed"),
            part_with("P-7", "Overlay roller bearing"),
            part_with("SP-1", "Stock 1 bearing"),
        ],
    )
    await _put(indexed, runtime, OVERLAY, [RecordDelete(kind=PART, key="SP-2")])
    await _put(
        indexed,
        runtime,
        "main",
        [
            part_with("P-1", "Tapered washer"),
            part_with("P-8", "Main only bearing"),
            supplier("S-1", "Alpha Seals", vat="IT1", rating="5"),
        ],
    )
    async with indexed() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await _assert_parity(session, OVERLAY)
        await _assert_parity(session, "main")
        hits = await search_records(session, DOMAIN, OVERLAY, "bearing", limit=50, backend="bm25")
        keys = {h.key for h in hits}
        assert {"P-1", "P-3", "P-7", "SP-1"} <= keys
        assert "P-8" not in keys


async def test_bm25_cuts_are_reported(indexed: Sessions) -> None:
    """A query matched as a superset is ranked over its first candidates: the result says
    when that may have left out a better match."""
    stock = json.dumps({"term": {"field": "terms", "value": "stock"}})
    call = text(
        "SELECT key, truncated FROM lb_search_bm25(:d, 'main', NULL, 'simple', 'stock', "
        "CAST(:candidates AS integer), CAST(:limit AS integer), CAST(:m AS jsonb), false)"
    )
    async with indexed() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        phrase = await search_records(session, DOMAIN, "main", '"stock 3"', limit=5, backend="bm25")
        assert [h.key for h in phrase] == ["SP-3"]
        assert not phrase.truncated

        async def run(candidates: int, limit: int) -> list[Any]:
            params = {"d": DOMAIN, "m": stock, "candidates": candidates, "limit": limit}
            return list((await session.execute(call, params)).all())

        # One candidate for five hits: fewer rows than asked, more may exist.
        assert [t for _, t in await run(1, 5)] == [True]
        # Two candidates for one hit: the best of them is the best of all.
        assert [t for _, t in await run(2, 1)] == [False]
        # Every match is a candidate: nothing is left out.
        assert not any(t for _, t in await run(100, 50))


async def test_bm25_runs_under_row_level_security_through_the_definer_functions(
    engine: AsyncEngine, indexed: Sessions
) -> None:
    async with reader_role(engine, functions=True) as role:
        async with indexed() as session, session.begin():
            await session.execute(text(f"SET LOCAL ROLE {role}"))
            await scope_to_domain(session, DOMAIN)
            hits = await search_records(session, DOMAIN, "main", "bearing", limit=5, backend="bm25")
            assert hits.backend == "bm25"
            assert {h.key for h in hits} == {"P-1", "P-3"}
        async with indexed() as session, session.begin():
            await session.execute(text(f"SET LOCAL ROLE {role}"))
            await scope_to_domain(session, "rival")
            with pytest.raises(DBAPIError) as refused:
                await search_records(session, DOMAIN, "main", "bearing", limit=5, backend="bm25")
            assert sqlstate(refused.value) == "42501"


async def test_storage_apply_drops_the_index_and_search_falls_back(
    engine: AsyncEngine, indexed: Sessions
) -> None:
    async with engine.connect() as connection:
        assert await bm25.status(connection) == bm25.Bm25Status(True)
    # Dropped behind the back of a process that still trusts it: that search falls back
    # within its transaction, and the next ones look again.
    async with indexed() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        cached = await search_records(session, DOMAIN, "main", "bearing", limit=5, backend="bm25")
        assert cached.backend == "bm25"
    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await auto.exec_driver_sql(f"DROP INDEX {bm25.INDEX}")
    async with indexed() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        fallen = await search_records(session, DOMAIN, "main", "bearing", limit=5, backend="bm25")
        assert fallen.backend == "postgres"
        assert {h.key for h in fallen} == {"P-1", "P-3"}
        assert await session.scalar(text("SELECT 1")) == 1  # the transaction goes on
    await _apply(engine, True)
    report = await _apply(engine, False)
    assert [(s.step, s.status) for s in report.steps if s.step == "bm25 index"] == [
        ("bm25 index", "done")
    ]
    async with engine.connect() as connection:
        found = await bm25.status(connection)
    assert not found.ready
    assert bm25.INDEX in (found.reason or "")
    async with indexed() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        hits = await search_records(session, DOMAIN, "main", "bearing", limit=5, backend="bm25")
        assert hits.backend == "postgres"
        assert {h.key for h in hits} == {"P-1", "P-3"}


# Everywhere: checks before the index, and the fallback -----------------------------------------

_CALL = (
    "SELECT * FROM {name}(:domain, :branch, NULL, 'simple', 'bearing', "
    "CAST(:candidates AS integer), CAST(:limit AS integer), CAST(:match AS jsonb), "
    "CAST(:exact AS boolean))"
)
_TERM = {"term": {"field": "terms", "value": "bearing"}}


async def _refusal(session: AsyncSession, name: str, **given: Any) -> str | None:
    params = {
        "domain": DOMAIN,
        "branch": "main",
        "candidates": 10,
        "limit": 5,
        "match": json.dumps(_TERM),
        "exact": True,
        **given,
    }
    try:
        async with session.begin_nested():
            await session.execute(text(_CALL.format(name=name)), params)
    except DBAPIError as error:
        return sqlstate(error)
    return None


@pytest.fixture
async def seeded(engine: AsyncEngine, runtime: StoreRuntime) -> Sessions:
    return await _seed(engine, runtime)


async def test_the_bm25_functions_check_their_arguments_first(
    engine: AsyncEngine, seeded: Sessions
) -> None:
    ceilings = StoreSettings().reads.definer_ceilings
    async with (
        reader_role(engine, functions=True) as role,
        seeded() as session,
        session.begin(),
    ):
        await session.execute(text(f"SET LOCAL ROLE {role}"))
        for name in ("lb_search_bm25", "lb_search_bm25_overlay"):
            assert await _refusal(session, name) == "42501", name
        await scope_to_domain(session, DOMAIN)
        for name in ("lb_search_bm25", "lb_search_bm25_overlay"):
            for given in (
                {"limit": 0},
                {"limit": ceilings.limit + 1},
                {"candidates": None},
                {"exact": None},
                {"match": None},
                {"match": json.dumps({"term": {"field": "domain_id", "value": "rival"}})},
                {"match": json.dumps({"parse": {"query_string": "terms:bearing"}})},
                {"match": json.dumps({"term": {"field": "terms", "value": 1}})},
                {"match": json.dumps({"boolean": {"must": [_TERM], "filter": [_TERM]}})},
                {"match": json.dumps({"all": None, "term": _TERM["term"]})},
            ):
                assert await _refusal(session, name, **given) == "22023", (name, given)


async def test_the_query_check_accepts_what_the_store_builds(engine: AsyncEngine) -> None:
    queries = [
        "'a'",
        "'a' & !'b' | 'c'",
        "'a' <-> 'b' <-> 'c'",
        "( 'a' | 'b' ) <2> !'c'",
        "!'a'",
        "'pre':*",
    ]
    async with engine.connect() as connection:
        for query in queries:
            built = bm25.translate(query)
            assert built is not None, query
            valid = await connection.scalar(
                text("SELECT lb_bm25_query_valid(CAST(:q AS jsonb))"), {"q": built.json()}
            )
            assert valid is True, query
        deep: dict[str, Any] = _TERM
        for _ in range(40):
            deep = {"boolean": {"must": [deep]}}
        assert not await connection.scalar(
            text("SELECT lb_bm25_query_valid(CAST(:q AS jsonb))"), {"q": json.dumps(deep)}
        )
        for vector, expected in (
            ("to_tsvector('simple', 'b a b c')", "b a b c"),
            ("strip(to_tsvector('simple', 'b a b c'))", "a b c"),
            (
                "setweight(to_tsvector('simple', 'Title'), 'A') || to_tsvector('simple', 'x')",
                "title x",
            ),
            ("CAST('' AS tsvector)", None),
        ):
            found = await connection.scalar(text(f"SELECT lb_search_terms({vector})"))
            assert found == expected, vector


async def test_without_pg_search_search_keeps_the_default_ranking(
    engine: AsyncEngine, seeded: Sessions
) -> None:
    if await _pg_search(engine):
        pytest.skip("the server has pg_search: covered by the tests above")
    report = await _apply(engine, True)
    assert [s.status for s in report.steps if s.step == "bm25 index"] in (["skipped"], ["failed"])
    async with engine.connect() as connection:
        found = await bm25.status(connection)
    assert not found.ready
    async with seeded() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        hits = await search_records(session, DOMAIN, "main", "bearing", limit=5, backend="bm25")
        assert hits.backend == "postgres"
        assert {h.key for h in hits} == {"P-1", "P-3"}
