"""Validation of DSL queries against the fixture `supply` schema."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from lorebase_core.model import LinkDirection
from lorebase_core.query import (
    Comparison,
    Conjunction,
    FieldSource,
    FullText,
    LinkCount,
    LinkExists,
    Negation,
    Operator,
    Query,
    QueryError,
    QueryLimits,
    resolve_query,
    resolve_where,
    target_of_kind,
)

from ..model.supply import (
    LOCATED,
    PART,
    PLANT,
    PLANT_COUNTRY,
    RISK_SCORE,
    SUPPLIED_BY,
    SUPPLIER,
    SUPPLIER_COUNT,
    SUPPLIER_COUNTRY,
    TAGS,
    TIER,
    supply_schema,
)

SCHEMA = supply_schema()
LIMITS = QueryLimits()


def plan(document: dict[str, Any], limits: QueryLimits = LIMITS) -> Any:
    return resolve_query(Query.model_validate(document), SCHEMA, limits)


def issues(document: dict[str, Any], limits: QueryLimits = LIMITS) -> dict[str, str]:
    with pytest.raises(QueryError) as caught:
        plan(document, limits)
    return {issue.pointer: issue.code for issue in caught.value.issues}


def test_attribute_names_resolve_to_ids_and_operands_to_canonical_values() -> None:
    resolved = plan(
        {
            "kind": "Supplier",
            "where": {
                "and": [
                    {"attr": "risk_score", "op": "gte", "value": 40},
                    {"attr": "tier", "op": "in", "value": ["1", "2", "1"]},
                    {"attr": "country", "op": "exists"},
                    {"match": "alloys"},
                ]
            },
            "order_by": [{"attr": "risk_score", "dir": "desc"}],
            "select": ["key", "title", "country"],
        }
    )
    assert resolved.target.kind_ids == (SUPPLIER,)
    assert resolved.limit == LIMITS.default_limit
    where = resolved.where
    assert isinstance(where, Conjunction)
    risk, tier, country, text = where.items
    assert isinstance(risk, Comparison)
    assert dict(risk.field.attributes) == {SUPPLIER: RISK_SCORE}
    assert risk.operand == "40"  # integers are canonical strings
    assert isinstance(tier, Comparison)
    assert tier.operand == ("1", "2")
    assert dict(tier.field.attributes) == {SUPPLIER: TIER}
    assert isinstance(country, Comparison)
    assert country.op is Operator.EXISTS
    assert country.operand is True
    assert text == FullText("alloys")
    assert resolved.order[0].descending
    assert [ref.source for ref in resolved.select] == [
        FieldSource.KEY,
        FieldSource.TITLE,
        FieldSource.VALUE,
    ]
    assert dict(resolved.select[2].attributes) == {SUPPLIER: SUPPLIER_COUNTRY}


def test_relation_predicates_resolve_both_directions() -> None:
    resolved = plan(
        {
            "kind": "Supplier",
            "where": {
                "and": [
                    {"via": "supplies", "where": {"attr": "name", "op": "prefix", "value": "Ta"}},
                    {"count": "supplies", "op": "between", "value": [1, 3]},
                    {"not": {"via": "contracts"}},
                ]
            },
        }
    )
    via, count, negated = resolved.where.items
    assert isinstance(via, LinkExists)
    assert via.hop.relation.id == SUPPLIED_BY
    assert via.hop.direction is LinkDirection.INVERSE
    assert via.hop.near_kind == SUPPLIER
    assert via.hop.far.kind_ids == (PART,)
    assert isinstance(count, LinkCount)
    assert count.operand == (1, 3)
    assert isinstance(negated, Negation)


def test_trait_targets_map_trait_attributes_per_kind() -> None:
    resolved = plan({"trait": "Located", "where": {"attr": "country", "op": "eq", "value": "IT"}})
    assert resolved.target.trait is not None
    assert resolved.target.trait.id == LOCATED
    assert set(resolved.target.kind_ids) == {SUPPLIER, PLANT}
    assert dict(resolved.where.field.attributes) == {
        SUPPLIER: SUPPLIER_COUNTRY,
        PLANT: PLANT_COUNTRY,
    }


def test_derived_attributes_and_arrays() -> None:
    resolved = plan(
        {
            "kind": "Part",
            "where": {
                "and": [
                    {"derived": "supplier_count", "op": "gt", "value": 1},
                    {"attr": "tags", "op": "contains", "value": "steel"},
                ]
            },
        }
    )
    derived, tags = resolved.where.items
    assert derived.field.source is FieldSource.DERIVED
    assert dict(derived.field.attributes) == {PART: SUPPLIER_COUNT}
    assert dict(tags.field.attributes) == {PART: TAGS}
    assert tags.operand == ("steel",)


def test_every_problem_is_reported_with_a_pointer() -> None:
    found = issues(
        {
            "kind": "Supplier",
            "where": {
                "or": [
                    {"attr": "nope", "op": "eq", "value": 1},
                    {"attr": "risk_score", "op": "prefix", "value": "4"},
                    {"attr": "risk_score", "op": "eq", "value": "high"},
                    {"via": "made_at"},
                    {"via": "unknown_relation"},
                    {"derived": "risk_score", "op": "eq", "value": 1},
                    {"count": "supplies", "op": "gt", "value": -1},
                    {"attr": "tier", "op": "in", "value": []},
                ]
            },
            "order_by": [{"attr": "missing"}],
            "select": ["key", "missing"],
            "limit": 10_000,
        }
    )
    assert found == {
        "/where/or/0/attr": "unknown_field",
        "/where/or/1/op": "operator_not_allowed",
        "/where/or/2/value": "invalid_value",
        "/where/or/3/via": "relation_not_on_kind",
        "/where/or/4/via": "unknown_relation",
        "/where/or/5/derived": "not_derived",
        "/where/or/6/value": "invalid_value",
        "/where/or/7/value": "invalid_value",
        "/order_by/0/attr": "unknown_field",
        "/select/1": "unknown_field",
        "/limit": "limit_exceeded",
    }


def test_unknown_kind_and_trait() -> None:
    assert issues({"kind": "Nope"}) == {"/kind": "unknown_kind"}
    assert issues({"trait": "Nope"}) == {"/trait": "unknown_trait"}


def test_limits_bound_depth_size_and_includes() -> None:
    tight = QueryLimits(max_via_depth=1, max_predicates=3, max_in_values=2, include_max_limit=4)
    deep = {"via": "supplies", "where": {"via": "supplied_by"}}
    assert issues({"kind": "Supplier", "where": deep}, tight) == {
        "/where/where/via": "depth_exceeded"
    }
    wide = {"and": [{"attr": "tier", "op": "eq", "value": "1"}] * 3}
    assert issues({"kind": "Supplier", "where": wide}, tight) == {"/where/and/2": "too_complex"}
    many = {"attr": "tier", "op": "in", "value": ["1", "2", "3"]}
    assert issues({"kind": "Supplier", "where": many}, tight) == {"/where/value": "limit_exceeded"}
    include = {"supplies": {"limit": 5}}
    assert issues({"kind": "Supplier", "include": include}, tight) == {
        "/include/supplies/limit": "limit_exceeded"
    }


def test_operators_follow_the_attribute_type() -> None:
    assert issues({"kind": "Part", "where": {"attr": "tags", "op": "eq", "value": "x"}}) == {
        "/where/op": "operator_not_allowed"
    }
    assert issues({"kind": "Part", "order_by": [{"attr": "tags"}]}) == {
        "/order_by/0/attr": "not_orderable"
    }
    assert issues({"kind": "Supplier", "where": {"attr": "tier", "op": "lt", "value": "2"}}) == {
        "/where/op": "operator_not_allowed"
    }


@pytest.mark.parametrize(
    "document",
    [
        {"kind": "Part", "trait": "Located"},
        {"where": {"attr": "x", "op": "eq", "value": 1}},
        {"kind": "Part", "where": {"unknown": 1}},
        {"kind": "Part", "where": {"attr": "x", "op": "like", "value": 1}},
        {"kind": "Part", "limit": -1},
        {"kind": "Part", "extra": True},
    ],
)
def test_malformed_documents_are_rejected_by_the_model(document: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Query.model_validate(document)


def test_standalone_predicates_resolve_against_a_target() -> None:
    target = target_of_kind(SCHEMA, "Supplier")
    assert target is not None
    condition = resolve_where(
        Query.model_validate(
            {"kind": "Supplier", "where": {"attr": "tier", "op": "eq", "value": "1"}}
        ).where,  # type: ignore[arg-type]
        target,
        SCHEMA,
        LIMITS,
    )
    assert isinstance(condition, Comparison)
    with pytest.raises(QueryError):
        resolve_where(
            Query.model_validate(
                {"kind": "Supplier", "where": {"attr": "x", "op": "eq", "value": 1}}
            ).where,  # type: ignore[arg-type]
            target,
            SCHEMA,
            LIMITS,
        )
