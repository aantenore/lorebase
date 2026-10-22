"""Neighborhood traversal (lorebase_store.traverse)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import (
    Derivation,
    LinkDirection,
    PathDerivation,
    PathStep,
    Schema,
    new_id,
)
from lorebase_core.model.ids import IdPrefix
from lorebase_store.commit import create_domain
from lorebase_store.reads import Direction, head_schema
from lorebase_store.repositories import scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import ReadSettings, StoreSettings, TraverseSettings
from lorebase_store.traverse import (
    Neighborhood,
    TraversalLimitError,
    neighborhood,
    neighborhood_sql,
    plan_hops,
)

from .read_support import MAIN_SUPPLIER, SOURCED_FROM, seed_graph, sourced_from, sourced_part
from .support import AUTHOR, DOMAIN, PART, SUPPLIED_BY, SUPPLIER, Clock, domain, supplied_by

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]


def nodes(found: Neighborhood) -> list[tuple[int, str]]:
    return [(n.depth, n.record.key) for n in found.records]


def test_statement_follows_the_schema() -> None:
    schema = Schema(
        domain=domain(),
        kinds={k.id: k for k in (sourced_part(),)},
        relations={r.id: r for r in (supplied_by(), sourced_from())},
    )
    sql, params = neighborhood_sql(schema, DOMAIN, "main", Direction.OUT, None)
    assert params["out_ids"] == [SUPPLIED_BY]
    assert params["in_ids"] == []
    assert params["fk_ids"] == [SOURCED_FROM]
    assert "CYCLE kind_id, key" in sql
    assert "w.kind_id = " not in sql, "inbound foreign-key hops only when walking in"
    sql, params = neighborhood_sql(schema, DOMAIN, "main", Direction.IN, [SOURCED_FROM])
    assert params["out_ids"] == params["in_ids"] == params["fk_ids"] == []
    assert f"w.kind_id = '{SUPPLIER}'" in sql
    follow = {SUPPLIED_BY: Direction.OUT, SOURCED_FROM: Direction.IN}
    sql, params = neighborhood_sql(schema, DOMAIN, "main", Direction.BOTH, None, follow=follow)
    assert params["out_ids"] == [SUPPLIED_BY]
    assert params["in_ids"] == params["fk_ids"] == []
    assert f"w.kind_id = '{SUPPLIER}'" in sql, "each Relation in its own direction"
    _, params = neighborhood_sql(
        schema,
        DOMAIN,
        "main",
        Direction.OUT,
        None,
        follow={SUPPLIED_BY: Direction.IN, SOURCED_FROM: Direction.OUT},
    )
    assert (params["out_ids"], params["in_ids"], params["fk_ids"]) == (
        [],
        [SUPPLIED_BY],
        [SOURCED_FROM],
    )
    hops = plan_hops(schema, follow={SOURCED_FROM: Direction.BOTH})
    assert json.loads(hops.as_json()) == {
        "out": [],
        "in": [],
        "fk_out": [[SOURCED_FROM, PART, SUPPLIER, MAIN_SUPPLIER]],
        "fk_in": [[SOURCED_FROM, PART, SUPPLIER, MAIN_SUPPLIER]],
    }


@pytest.fixture(params=[True, False], ids=["definer", "plain"])
def store(request: pytest.FixtureRequest) -> StoreSettings:
    """Every walk runs through `lb_neighborhood` and as plain SQL, with the same results."""
    return StoreSettings(reads=ReadSettings(use_definer_functions=request.param))


@pytest.fixture
async def walk(engine: AsyncEngine, runtime: StoreRuntime) -> Sessions:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=Clock())
    async with factory() as session:
        await seed_graph(session, runtime)
    return factory


async def around(
    sessions: Sessions, runtime: StoreRuntime, seeds: list[tuple[str, str]], **options: object
) -> Neighborhood:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = await head_schema(session, DOMAIN, "main", runtime=runtime)
        assert found is not None
        return await neighborhood(
            session,
            DOMAIN,
            "main",
            found.schema,
            seeds,
            runtime=runtime,
            **options,  # type: ignore[arg-type]
        )


async def test_two_hops_both_ways(walk: Sessions, runtime: StoreRuntime) -> None:
    found = await around(walk, runtime, [(PART, "P-1")], depth=2)
    assert nodes(found) == [(0, "P-1"), (1, "S-1"), (1, "S-2"), (2, "P-2"), (2, "P-3"), (2, "P-4")]
    assert [(n.record.kind_id, n.record.title) for n in found.records[:2]] == [
        (PART, "Tapered roller bearing"),
        (SUPPLIER, "Alpha Bearings"),
    ]
    assert [(link.relation_id, link.from_key, link.to_key) for link in found.links] == [
        (SUPPLIED_BY, "P-1", "S-1"),
        (SUPPLIED_BY, "P-2", "S-1"),
        (SUPPLIED_BY, "P-3", "S-1"),
        (SUPPLIED_BY, "P-4", "S-2"),
        (SOURCED_FROM, "P-1", "S-2"),
    ]
    assert not found.truncated


async def test_direction_relations_and_foreign_keys(walk: Sessions, runtime: StoreRuntime) -> None:
    inbound = await around(walk, runtime, [(SUPPLIER, "S-2")], depth=1, direction=Direction.IN)
    assert nodes(inbound) == [(0, "S-2"), (1, "P-1"), (1, "P-4")]
    outbound = await around(walk, runtime, [(SUPPLIER, "S-2")], depth=3, direction=Direction.OUT)
    assert nodes(outbound) == [(0, "S-2")]
    filtered = await around(walk, runtime, [(PART, "P-1")], depth=2, relation_ids=[SOURCED_FROM])
    assert nodes(filtered) == [(0, "P-1"), (1, "S-2")]
    assert [link.object_hash for link in filtered.links] == [None]
    follow = {SUPPLIED_BY: Direction.IN}
    pinned = await around(walk, runtime, [(SUPPLIER, "S-2")], depth=1, follow=follow)
    assert nodes(pinned) == [(0, "S-2"), (1, "P-4")]
    missing = await around(walk, runtime, [(PART, "P-404")], depth=2)
    assert missing.records == []
    assert missing.links == []


async def test_follow_names_relation_and_direction(walk: Sessions, runtime: StoreRuntime) -> None:
    forward = await around(
        walk, runtime, [(PART, "P-1")], depth=2, follow={SUPPLIED_BY: Direction.OUT}
    )
    assert nodes(forward) == [(0, "P-1"), (1, "S-1")]
    both = await around(
        walk,
        runtime,
        [(PART, "P-1")],
        depth=2,
        follow={SUPPLIED_BY: Direction.BOTH},
    )
    assert nodes(both) == [(0, "P-1"), (1, "S-1"), (2, "P-2"), (2, "P-3")]
    backward = await around(
        walk, runtime, [(SUPPLIER, "S-2")], depth=1, follow={SOURCED_FROM: Direction.IN}
    )
    assert nodes(backward) == [(0, "S-2"), (1, "P-1")]
    assert not backward.truncated


async def test_caps(walk: Sessions, engine: AsyncEngine, store: StoreSettings) -> None:
    runtime = StoreRuntime.from_settings(
        StoreSettings(
            traverse=TraverseSettings(max_depth=2, max_nodes=3, max_fanout=5), reads=store.reads
        )
    )
    capped = await around(walk, runtime, [(PART, "P-1")], depth=2)
    assert nodes(capped) == [(0, "P-1"), (1, "S-1"), (1, "S-2")]
    assert capped.truncated
    assert {(link.from_key, link.to_key) for link in capped.links} == {
        ("P-1", "S-1"),
        ("P-1", "S-2"),
    }
    narrow = await around(walk, runtime, [(SUPPLIER, "S-1")], depth=1, fanout=1)
    assert len(narrow.records) == 2
    assert narrow.truncated, "S-1 has three Links, one was followed"
    exact = await around(walk, runtime, [(SUPPLIER, "S-2")], depth=1, fanout=3)
    assert len(exact.records) == 3
    assert not exact.truncated, "S-2 has two Links, both followed"
    few_links = await around(walk, runtime, [(SUPPLIER, "S-1")], depth=2, max_links=2)
    assert few_links.truncated
    assert len(few_links.records) == 3
    with pytest.raises(TraversalLimitError, match="depth 3 exceeds"):
        await around(walk, runtime, [(PART, "P-1")], depth=3)
    with pytest.raises(TraversalLimitError, match="max_nodes 4 exceeds"):
        await around(walk, runtime, [(PART, "P-1")], max_nodes=4)


async def test_a_composed_path_is_truncated_only_when_links_are_left_out(
    walk: Sessions, runtime: StoreRuntime
) -> None:
    """`co_supplied` (supplied_by, then back) reaches P-1, P-2 and P-3 from P-1 through S-1:
    three Links, all followed at fan-out 3, one left out at fan-out 2."""
    co_supplied = Derivation(
        id=new_id(IdPrefix.DERIVATION),
        api_name="co_supplied",
        definition=PathDerivation(
            steps=(
                PathStep(relation=SUPPLIED_BY),
                PathStep(relation=SUPPLIED_BY, direction=LinkDirection.INVERSE),
            ),
        ),
    )

    async def composed(fanout: int) -> Neighborhood:
        async with walk() as session, session.begin():
            await scope_to_domain(session, DOMAIN)
            found = await head_schema(session, DOMAIN, "main", runtime=runtime)
            assert found is not None
            derivations = {**found.schema.derivations, co_supplied.id: co_supplied}
            return await neighborhood(
                session,
                DOMAIN,
                "main",
                found.schema.model_copy(update={"derivations": derivations}),
                [(PART, "P-1")],
                depth=1,
                follow={co_supplied.id: Direction.OUT},
                fanout=fanout,
                runtime=runtime,
            )

    exact = await composed(3)
    assert {(link.from_key, link.to_key) for link in exact.links} == {
        ("P-1", "P-1"),
        ("P-1", "P-2"),
        ("P-1", "P-3"),
    }
    assert not exact.truncated, "three Links, all followed"
    narrow = await composed(2)
    assert len(narrow.links) == 2
    assert narrow.truncated, "three Links, two followed"
