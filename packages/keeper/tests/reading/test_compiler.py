"""Golden tests of the query compiler: SQL text and bound parameters of resolved DSL queries.

Snapshots live next to this file in `snapshots/`; set LOREBASE_UPDATE_SNAPSHOTS=1 to rewrite
them after an intended change, then review the diff.
"""

from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import ClauseElement

from lorebase_core.model.yaml_schema import load_schema
from lorebase_core.query import Query, QueryLimits, QueryPlan, resolve_query
from lorebase_keeper.reading.compiler import (
    Compiler,
    after,
    cursor_value,
    from_cursor_value,
    order_clauses,
    record_columns,
)

SCHEMA = load_schema(
    (Path(__file__).resolve().parents[4] / "examples" / "supply" / "schema.yaml").read_text()
)
SNAPSHOTS = Path(__file__).parent / "snapshots"
UPDATE = os.environ.get("LOREBASE_UPDATE_SNAPSHOTS") == "1"
COMPILER = Compiler("supply", "main", text_search_config="simple")


def resolve(document: dict[str, Any]) -> QueryPlan:
    return resolve_query(Query.model_validate(document), SCHEMA, QueryLimits())


def list_statement(plan: QueryPlan, position: list[Any] | None = None) -> ClauseElement:
    r = COMPILER.records("r")
    keys = COMPILER.sort_keys(r, plan.order, plan.target)
    query = select(*record_columns(r), *(k.expression.label(f"s{i}") for i, k in enumerate(keys)))
    query = query.where(COMPILER.record_scope(r, plan.target))
    if plan.where is not None:
        query = query.where(COMPILER.condition(r, plan.where, plan.target))
    if position is not None:
        values = [from_cursor_value(v, k.type) for k, v in zip(keys, position, strict=True)]
        query = query.where(after(keys, values))
    return query.order_by(*order_clauses(keys)).limit(plan.limit + 1)


def render(statement: ClauseElement) -> str:
    compiled = statement.compile(dialect=postgresql.dialect())
    params = {name: _jsonable(value) for name, value in sorted(compiled.params.items())}
    return f"{compiled}\n-- params\n{json.dumps(params, indent=2, sort_keys=True)}\n"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal | date):
        return f"{type(value).__name__}:{value}"
    return value


def check(name: str, statement: ClauseElement) -> str:
    rendered = render(statement)
    path = SNAPSHOTS / f"{name}.sql"
    if UPDATE or not path.exists():
        path.parent.mkdir(exist_ok=True)
        path.write_text(rendered)
        if not UPDATE:
            pytest.fail(f"snapshot {path.name} was missing and has been written; rerun")
    assert rendered == path.read_text()
    return rendered


def test_filter_containment_and_keyset_after_a_cursor() -> None:
    plan = resolve(
        {
            "kind": "Supplier",
            "where": {
                "and": [
                    {"attr": "country", "op": "eq", "value": "PL"},
                    {"attr": "risk_score", "op": "between", "value": [10, 80]},
                    {"attr": "legal_name", "op": "prefix", "value": "Kav%_"},
                    {"attr": "vat_number", "op": "exists", "value": False},
                ]
            },
            "order_by": [{"attr": "risk_score", "dir": "desc"}],
            "limit": 20,
        }
    )
    sql = check("supplier_filter_keyset", list_statement(plan, [False, "38", "S-1002"]))
    assert "Kav" not in sql.split("-- params")[0]


def test_relation_existence_count_and_full_text() -> None:
    plan = resolve(
        {
            "kind": "Part",
            "where": {
                "and": [
                    {
                        "via": "supplied_by",
                        "where": {"attr": "country", "op": "in", "value": ["IT", "DE"]},
                    },
                    {"count": "supplied_by", "op": "gte", "value": 2},
                    {"not": {"match": "obsolete seal"}},
                ]
            },
        }
    )
    check("part_relations", list_statement(plan))


def test_foreign_key_relation_and_dates() -> None:
    plan = resolve(
        {
            "kind": "Supplier",
            "where": {
                "or": [
                    {
                        "via": "purchase_orders",
                        "where": {"attr": "due_on", "op": "lt", "value": "2026-01-01"},
                    },
                    {"attr": "onboarded_on", "op": "gte", "value": "2024-01-01"},
                ]
            },
            "order_by": [{"attr": "onboarded_on"}],
        }
    )
    check("supplier_foreign_key", list_statement(plan))


def test_trait_target_reads_each_kind_by_its_own_attribute() -> None:
    plan = resolve(
        {
            "trait": "Located",
            "where": {"attr": "country", "op": "ne", "value": "IT"},
            "order_by": [{"attr": "city"}],
        }
    )
    check("located_trait", list_statement(plan))


def test_include_neighbors_query() -> None:
    plan = resolve({"kind": "Part", "include": {"supplied_by": {"limit": 3}}})
    (include,) = plan.include
    check("part_include", COMPILER.neighbors(include.hop, ["P-1", "P-2"], per_near=include.limit))


def test_values_never_enter_the_sql_text() -> None:
    hostile = "x'); DROP TABLE mat_records; --"
    plan = resolve(
        {
            "kind": "Supplier",
            "where": {
                "or": [
                    {"attr": "city", "op": "eq", "value": hostile},
                    {"attr": "city", "op": "prefix", "value": hostile},
                    {"attr": "city", "op": "in", "value": [hostile]},
                    {"match": hostile},
                    {"via": "supplies", "where": {"attr": "name", "op": "ne", "value": hostile}},
                ]
            },
        }
    )
    statement = list_statement(plan).compile(dialect=postgresql.dialect())
    assert "DROP" not in str(statement)
    assert any(hostile in json.dumps(v) for v in statement.params.values())


@pytest.mark.parametrize(
    "value",
    [Decimal("12.50"), date(2026, 1, 2), True, "S-1", 1.5, None],
)
def test_cursor_values_round_trip(value: Any) -> None:
    from sqlalchemy import Boolean, Date, Numeric, Text
    from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION

    sql_type = {
        Decimal: Numeric(),
        date: Date(),
        bool: Boolean(),
        str: Text(),
        float: DOUBLE_PRECISION(),
        type(None): Text(),
    }[type(value)]
    assert from_cursor_value(json.loads(json.dumps(cursor_value(value))), sql_type) == value


def test_malformed_cursor_values_are_rejected() -> None:
    from sqlalchemy import Boolean, Numeric

    with pytest.raises(ValueError, match="boolean"):
        from_cursor_value("yes", Boolean())
    with pytest.raises(ValueError, match="malformed"):
        from_cursor_value("not a number", Numeric())
