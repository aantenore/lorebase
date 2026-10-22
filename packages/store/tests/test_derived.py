"""Aggregates and the evaluation order of Derivations, without a database."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from lorebase_core.expr import ExprLimits, LinkedRecord
from lorebase_core.model import (
    AggregateDerivation,
    AggregateFunction,
    Attribute,
    LinkDirection,
    Schema,
    load_schema,
)
from lorebase_core.types import TypeSpec
from lorebase_store.materialize.derived import DerivedValues, aggregate, plan_derivations

SUPPLY = Path(__file__).resolve().parents[3] / "examples" / "supply" / "schema.yaml"


@pytest.fixture(scope="module")
def schema() -> Schema:
    return load_schema(SUPPLY.read_text())


class _Repository:
    """Records of a Kind by key; the first ones are read as one page, never as a stream."""

    def __init__(self) -> None:
        self.pages: list[tuple[str, str | None, int]] = []

    async def record_page(self, kind_id: str, *, after: str | None, limit: int) -> list[Any]:
        self.pages.append((kind_id, after, limit))
        return [SimpleNamespace(key=f"P-{n}") for n in range(limit)]

    def stream_records(self, kind_id: str, *, batch: int) -> Any:
        raise AssertionError("a preview reads one page")

    async def records(self, kind_id: str, keys: Any) -> dict[str, Any]:
        return {}


async def test_a_preview_reads_the_first_records_as_one_page(schema: Schema) -> None:
    repository = _Repository()
    values = DerivedValues(
        cast(Any, repository),
        schema,
        limits=ExprLimits(),
        now=datetime(2026, 1, 1, tzinfo=UTC),
        batch=100,
        max_incremental=100,
        fanout=100,
    )
    single = schema.kind_by_api_name("Part")
    assert single is not None
    attribute = single.attribute_by_api_name("single_sourced")
    assert attribute is not None
    assert await values.preview(attribute.id, keys=None, limit=3) == []
    assert repository.pages == [(single.id, None, 3)]


def test_derivations_run_in_dependency_order(schema: Schema) -> None:
    steps = plan_derivations(schema, ExprLimits())
    assert [s.derivation.api_name for s in steps] == ["supplier_count", "single_sourced"]
    assert [h.relation.api_name for h in steps[0].hops] == ["supplied_by"]
    assert steps[1].hops == ()


@pytest.mark.parametrize(
    ("function", "target", "expected"),
    [
        (AggregateFunction.COUNT, TypeSpec.of("integer"), "3"),
        (AggregateFunction.SUM, TypeSpec(name="decimal", precision=10, scale=2), "30.50"),
        (AggregateFunction.AVG, TypeSpec(name="decimal", precision=10, scale=2), "10.17"),
        (AggregateFunction.MIN, TypeSpec(name="decimal", precision=10, scale=2), "0.50"),
        (AggregateFunction.MAX, TypeSpec(name="decimal", precision=10, scale=2), "20.00"),
    ],
)
def test_aggregates(
    schema: Schema, function: AggregateFunction, target: TypeSpec, expected: str
) -> None:
    supplier = schema.kind_by_api_name("Supplier")
    part = schema.kind_by_api_name("Part")
    found = schema.relation_by_api_name("supplied_by")
    assert supplier is not None
    assert part is not None
    assert found is not None
    cost = part.attribute_by_api_name("unit_cost")
    assert cost is not None
    attribute = Attribute(
        id="att_01KF0000000000000000000099", api_name="parts_cost", type=target, description="x"
    )
    definition = AggregateDerivation(
        kind=supplier.id,
        attribute=attribute.id,
        relation=found[0].id,
        direction=LinkDirection.INVERSE,
        function=function,
        source_attribute=None if function is AggregateFunction.COUNT else cost.id,
    )
    members = [
        LinkedRecord("P-1", {cost.id: "10.00"}),
        LinkedRecord("P-2", {cost.id: "20.00"}),
        LinkedRecord("P-3", {cost.id: "0.50"}),
    ]
    assert aggregate(attribute, definition, part, members) == expected
    if function is not AggregateFunction.COUNT and function is not AggregateFunction.SUM:
        assert aggregate(attribute, definition, part, []) is None
