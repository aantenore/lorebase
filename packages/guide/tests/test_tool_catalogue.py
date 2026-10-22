"""The tools as the model sees them: descriptions that match what Keeper accepts, and results
that carry what Keeper returned.

Live runs against hosted models (docs/validation.md, live model runs) showed most failed tool
calls came from the `query` description: it gave `where` as `{attr: value}` and `order_by` as
`{attr: asc|desc}`, shapes Keeper's query DSL refuses. The predicates and Keeper answers below
were recorded from those runs.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from annotated_types import Le, MaxLen
from pydantic import TypeAdapter, ValidationError

from lorebase_client import AsyncLorebaseClient
from lorebase_core.query.dsl import Predicate, Query
from lorebase_guide.prompts import FINAL_CALL, SYSTEM
from lorebase_guide.tools import (
    QUERY_EXAMPLE,
    TOOLS,
    HistoryArgs,
    NeighborhoodArgs,
    ToolContext,
    TraverseArgs,
    run_tool,
)

from .support import guide_settings

PREDICATE: TypeAdapter[Any] = TypeAdapter(Predicate)

RECORDED_REFUSED = [
    {"single_sourced": True},
    {"tier": "1", "risk_score": {"gte": 70}},
    {"attr": "single_sourced", "eq": True},
    {"match": {"attr": "single_sourced", "eq": True}},
    {"and": [{"attr": {"op": "eq", "value": "1"}}]},
]


def test_query_example_is_a_valid_keeper_query() -> None:
    example = json.loads(QUERY_EXAMPLE)
    Query.model_validate(example)
    assert QUERY_EXAMPLE in TOOLS["query"].description


@pytest.mark.parametrize("where", RECORDED_REFUSED)
def test_shapes_the_old_description_suggested_are_refused(where: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PREDICATE.validate_python(where)


def test_every_predicate_form_named_in_the_description_is_accepted() -> None:
    forms = [
        {"attr": "single_sourced", "op": "eq", "value": True},
        {"derived": "single_sourced", "op": "eq", "value": True},
        {"via": "supplied_by"},
        {"count": "supplied_by", "op": "gte", "value": 2},
        {"match": "bearing"},
        {"not": {"attr": "status", "op": "in", "value": ["on_hold", "terminated"]}},
    ]
    description = TOOLS["query"].description
    for form in forms:
        PREDICATE.validate_python(form)
        assert f'{{"{next(iter(form))}"' in description
    assert '"dir"' in description
    assert "predicate as in query" in TOOLS["traverse"].description


def _bound(model: type[Any], name: str) -> int:
    for meta in model.model_fields[name].metadata:
        if isinstance(meta, Le):
            return int(meta.le)
        if isinstance(meta, MaxLen):
            return int(meta.max_length)
    raise AssertionError(f"{model.__name__}.{name} has no upper bound")


def test_descriptions_state_the_bounds_the_arguments_enforce() -> None:
    assert f"1-{_bound(HistoryArgs, 'limit')}" in TOOLS["history"].description
    assert f"1-{_bound(NeighborhoodArgs, 'refs')}" in TOOLS["neighborhood"].description
    assert f"1-{_bound(TraverseArgs, 'path')} hops" in TOOLS["traverse"].description


def test_final_turn_asks_for_a_partial_answer_rather_than_none() -> None:
    assert "answer that part" in FINAL_CALL
    assert "found false only when nothing" in FINAL_CALL


def test_a_checked_negative_result_is_an_answer() -> None:
    """Live runs answered `Not found in the graph.` after checking that no tier-1 supplier
    met the conditions: the model treated "none" as "not found"."""
    assert "even when it is negative" in SYSTEM


# Keeper's answers for the stock of Part P-10012, recorded from the live runs (`supply` sample).
PLANT = {"kind": "Plant", "key": "PL-17", "title": "Poznan Assembly", "values": {"city": "Poznan"}}
PART = {"kind": "Part", "key": "P-10012", "title": "Temperature probe PT100 150 mm", "values": {}}
EDGE = {
    "relation": "stocked_at",
    "from": {"kind": "Part", "key": "P-10012", "title": None},
    "to": {"kind": "Plant", "key": "PL-17", "title": "Poznan Assembly"},
    "values": {"quantity": "1700"},
}


def _keeper(request: httpx.Request) -> httpx.Response:
    served = {"revision": "r1", "served_from": "materialized"}
    if request.url.path.endswith("/neighborhood"):
        return httpx.Response(
            200, json={**served, "nodes": [PART, PLANT], "edges": [EDGE], "truncated": False}
        )
    if request.url.path.endswith("/traverse"):
        hop = {"relation": "stocked_at", "records": [PLANT], "edges": [EDGE], "truncated": False}
        return httpx.Response(200, json={**served, "start": PART, "hops": [hop]})
    return httpx.Response(404, json={"status": 404, "title": "not found"})


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("neighborhood", {"refs": ["Part/P-10012"], "relations": ["stocked_at"]}),
        ("traverse", {"start": "Part/P-10012", "path": [{"relation": "stocked_at"}]}),
    ],
)
async def test_link_attributes_reach_the_model(tool: str, args: dict[str, Any]) -> None:
    """A live run answered that the graph holds no stock quantities: the neighborhood tool
    dropped the Link Attributes Keeper returned (`stocked_at.quantity`)."""
    async with AsyncLorebaseClient(
        "http://keeper.test", api_key="lbk_test_reader", transport=httpx.MockTransport(_keeper)
    ) as client:
        ctx = ToolContext(
            client=client,
            domain="supply",
            branch=None,
            settings=guide_settings().agent,
            embedder=None,
        )
        result = await run_tool(ctx, tool, args)

    assert result.ok, result.summary
    assert '"quantity": "1700"' in json.dumps(result.data)
