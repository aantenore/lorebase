"""Path Derivations as virtual Relations: resolution, name lookups and the shared walk."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from lorebase_core.model import Derivation, LinkDirection, PathDerivation, PathStep, Schema
from lorebase_core.model.paths import bounded, path_relation, walk
from lorebase_core.query import (
    Query,
    QueryLimits,
    plan_kinds,
    resolve_hop,
    resolve_query,
    target_of_kind,
)

from .supply import CONTRACT, MADE_AT, PART, PLANT, SUPPLIED_BY, WITH_SUPPLIER, ident, supply_schema

PART_CONTRACTS = ident("drv", 50)
BROKEN = ident("drv", 51)


def path(
    derivation_id: str, name: str, *steps: PathStep, inverse: str | None = None, **extra: bool
) -> Derivation:
    return Derivation(
        id=derivation_id,
        api_name=name,
        definition=PathDerivation(steps=steps, inverse_api_name=inverse, **extra),
    )


def schema_with(*derivations: Derivation) -> Schema:
    schema = supply_schema()
    return schema.model_copy(
        update={"derivations": {**schema.derivations, **{d.id: d for d in derivations}}}
    )


def part_contracts(**extra: bool) -> Derivation:
    return path(
        PART_CONTRACTS,
        "contracts_of_suppliers",
        PathStep(relation=SUPPLIED_BY),
        PathStep(relation=WITH_SUPPLIER, direction=LinkDirection.INVERSE),
        inverse="parts_under_contract",
        **extra,
    )


def test_a_path_resolves_to_a_virtual_relation() -> None:
    schema = schema_with(part_contracts(materialized=True))
    found = path_relation(schema.derivations[PART_CONTRACTS], schema.relations)
    assert found is not None
    assert found.materialized
    assert (found.relation.id, found.relation.from_kind, found.relation.to_kind) == (
        PART_CONTRACTS,
        PART,
        CONTRACT,
    )
    assert found.relation.attributes == {}
    assert found.inner_kinds == (schema.relations[SUPPLIED_BY].to_kind,)
    assert schema.path_by_api_name("contracts_of_suppliers") == (found, LinkDirection.FORWARD)
    inverse = schema.path_by_api_name("parts_under_contract")
    assert inverse is not None
    assert inverse[1] is LinkDirection.INVERSE
    assert schema.relation_by_api_name("contracts_of_suppliers") is None


def test_broken_paths_are_left_out() -> None:
    gap = path(BROKEN, "nowhere", PathStep(relation=SUPPLIED_BY), PathStep(relation=MADE_AT))
    schema = schema_with(gap)
    assert path_relation(gap, schema.relations) is None
    assert schema.path_by_api_name("nowhere") is None
    assert BROKEN not in schema.path_relations()


def test_hops_along_a_path_read_the_intermediate_kinds() -> None:
    schema = schema_with(part_contracts())
    part = target_of_kind(schema, "Part")
    assert part is not None
    hop = resolve_hop(schema, part, "contracts_of_suppliers")
    assert not isinstance(hop, str)
    assert hop.path is not None
    assert hop.far.kind_ids == (CONTRACT,)
    assert hop.kinds_read == {"Contract", "Supplier"}
    contract = target_of_kind(schema, "Contract")
    assert contract is not None
    back = resolve_hop(schema, contract, "parts_under_contract")
    assert not isinstance(back, str)
    assert (back.direction, back.far.kind_ids) == (LinkDirection.INVERSE, (PART,))
    assert isinstance(resolve_hop(schema, contract, "contracts_of_suppliers"), str)

    plan = resolve_query(
        Query.model_validate({"kind": "Part", "where": {"via": "contracts_of_suppliers"}}),
        schema,
        QueryLimits(),
    )
    assert plan_kinds(plan) == {"Part", "Supplier", "Contract"}


def lookup(links: Mapping[str, Sequence[str]]) -> object:
    return lambda keys: {k: links[k] for k in keys if k in links}


def test_walk_bounds_later_steps_per_intermediate_record() -> None:
    first = {"a": ["b1", "b2", "b3"], "x": ["b1"]}
    second = {"b1": ["c3", "c1", "c2"], "b2": ["c1"], "b3": ["a"]}
    reached = walk(["a", "x", "lonely"], [lookup(first), lookup(second)], 2)  # type: ignore[list-item]
    # b1 keeps its two smallest far keys whoever reaches it; a cycle back to a is a pair
    assert reached == {"a": {"c1", "c2", "a"}, "x": {"c1", "c2"}, "lonely": set()}
    unbounded = walk(["x"], [lookup(first), lookup(second)], None)  # type: ignore[list-item]
    assert unbounded == {"x": {"c1", "c2", "c3"}}
    assert bounded(["b", "a", "b"], 1) == ["a"]


def test_a_path_may_start_from_the_inverse_of_a_foreign_key() -> None:
    schema = schema_with(
        path(
            BROKEN,
            "plant_contracts",
            PathStep(relation=MADE_AT, direction=LinkDirection.INVERSE),
            PathStep(relation=SUPPLIED_BY),
        )
    )
    found = schema.path_relations()[BROKEN]
    assert found.kinds == (PLANT, PART, schema.relations[SUPPLIED_BY].to_kind)
