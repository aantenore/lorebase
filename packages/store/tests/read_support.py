"""A small graph for the read and traversal tests, plus SQL capture for plan checks.

Suppliers S-1..S-3; parts P-1..P-4 supplied by S-1 (P-1, P-2, P-3) and S-2 (P-4) through the
stored Relation `supplied_by`; P-1 names S-2 as `main_supplier`, which backs the foreign-key
Relation `sourced_from`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from lorebase_core.model import Cardinality, Kind, Record, Relation, RelationBacking
from lorebase_core.types import TypeSpec
from lorebase_core.validate import RecordUpsert
from lorebase_store import testing
from lorebase_store.commit import CommitResult, SchemaUpsert, commit
from lorebase_store.indexes import sql_literal
from lorebase_store.runtime import StoreRuntime

from .support import (
    AUTHOR,
    DOMAIN,
    ORIGIN,
    PART,
    PART_NAME,
    PART_NUMBER,
    RATING,
    SUPPLIER,
    attribute,
    ident,
    part_kind,
    supplied,
    supplied_by,
    supplier,
    supplier_kind,
)

MAIN_SUPPLIER = ident("att", 290)
SOURCED_FROM = ident("rel", 9)
T = TypeSpec.of


def rated_supplier() -> Kind:
    kind = supplier_kind(rating=T("integer"))
    attributes = dict(kind.attributes)
    attributes[RATING] = attributes[RATING].model_copy(update={"indexed": True})
    return kind.model_copy(update={"attributes": attributes})


def sourced_part() -> Kind:
    kind = part_kind()
    main = attribute(MAIN_SUPPLIER, "main_supplier", TypeSpec.ref(SUPPLIER))
    return kind.model_copy(update={"attributes": {**kind.attributes, MAIN_SUPPLIER: main}})


def sourced_from() -> Relation:
    return Relation(
        id=SOURCED_FROM,
        api_name="sourced_from",
        inverse_api_name="sources",
        description="The main supplier of a part.",
        from_kind=PART,
        to_kind=SUPPLIER,
        cardinality=Cardinality.MANY_TO_ONE,
        backing=RelationBacking.FOREIGN_KEY,
        foreign_key_attribute=MAIN_SUPPLIER,
    )


def part_with(number: str, name: str, **values: Any) -> RecordUpsert:
    record_values = {PART_NUMBER: number, PART_NAME: name}
    if "main_supplier" in values:
        record_values[MAIN_SUPPLIER] = values["main_supplier"]
    return RecordUpsert(record=Record(kind=PART, key=number, values=record_values))


async def seed_graph(
    session: AsyncSession, runtime: StoreRuntime, *, domain_id: str = DOMAIN, label: str = ""
) -> CommitResult:
    """The graph above in `domain_id`, every title starting with `label`."""
    return await commit(
        session,
        domain_id,
        "main",
        [
            supplier("S-1", f"{label}Alpha Bearings", vat="IT1", rating="5"),
            supplier("S-2", f"{label}Beta Seals", vat="IT2", rating="7"),
            supplier("S-3", f"{label}Gamma Filters", vat="IT3", rating="9"),
            part_with("P-1", f"{label}Tapered roller bearing", main_supplier="S-2"),
            part_with("P-2", f"{label}Flat washer"),
            part_with("P-3", f"{label}Ball bearing"),
            part_with("P-4", f"{label}Oil seal"),
            supplied("P-1", "S-1"),
            supplied("P-2", "S-1"),
            supplied("P-3", "S-1"),
            supplied("P-4", "S-2"),
        ],
        schema_changes=[
            SchemaUpsert(rated_supplier()),
            SchemaUpsert(sourced_part()),
            SchemaUpsert(supplied_by()),
            SchemaUpsert(sourced_from()),
        ],
        author=AUTHOR,
        message="graph",
        origin=ORIGIN,
        runtime=runtime,
    )


# Plans ------------------------------------------------------------------------------------------


@contextmanager
def captured(engine: AsyncEngine) -> Iterator[list[tuple[str, Any]]]:
    """Statements (driver SQL and parameters) executed on `engine` meanwhile."""
    statements: list[tuple[str, Any]] = []

    def record(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        statements.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


async def plan_of(
    connection: AsyncConnection, statement: str, parameters: Any, *, generic: bool = False
) -> dict[str, Any]:
    """EXPLAIN (FORMAT JSON) of a captured statement: with its parameters, or the generic
    plan a prepared statement settles on after a few executions."""
    if generic:
        await connection.exec_driver_sql("SET LOCAL plan_cache_mode = force_generic_plan")
        await connection.exec_driver_sql(f"PREPARE lb_plan_probe AS {statement}")
        arguments = ", ".join(_literal(value) for value in parameters)
        execute = f"EXECUTE lb_plan_probe({arguments})" if parameters else "EXECUTE lb_plan_probe"
        try:
            result = await connection.exec_driver_sql(f"EXPLAIN (FORMAT JSON) {execute}")
        finally:
            await connection.exec_driver_sql("DEALLOCATE lb_plan_probe")
            await connection.exec_driver_sql("SET LOCAL plan_cache_mode = auto")
    else:
        result = await connection.exec_driver_sql(f"EXPLAIN (FORMAT JSON) {statement}", parameters)
    raw = result.scalar_one()
    plan: dict[str, Any] = (json.loads(raw) if isinstance(raw, str) else raw)[0]["Plan"]
    return plan


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, bytes):
        return f"'\\x{value.hex()}'"
    if isinstance(value, list):
        return "ARRAY[" + ", ".join(_literal(v) for v in value) + "]::text[]" if value else "'{}'"
    return sql_literal(str(value))


@asynccontextmanager
async def nested_plans(session: AsyncSession) -> AsyncIterator[list[dict[str, Any]]]:
    """`lorebase_store.testing.nested_plans`, skipping the test where `auto_explain` cannot
    be loaded (a server role that is not a superuser)."""
    try:
        context = testing.nested_plans(session)
        plans = await context.__aenter__()
    except DBAPIError as error:
        pytest.skip(f"auto_explain is not available: {error.orig}")
    try:
        yield plans
    finally:
        await context.__aexit__(None, None, None)


def plan_nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield plan
    for child in plan.get("Plans", []):
        yield from plan_nodes(child)


def scans(plan: dict[str, Any]) -> list[tuple[str, str | None]]:
    """`(node type, index name)` of every scan of a table in the plan."""
    return [
        (node["Node Type"], node.get("Index Name"))
        for node in plan_nodes(plan)
        if "Relation Name" in node or "Index Name" in node
    ]


_LEXICON = text(
    "SELECT kind_id, word, records FROM mat_lexicon WHERE domain_id = :d AND branch = :b"
)
_LEXICON_RECOMPUTED = text(
    "SELECT m.kind_id, w.lexeme, count(*) FROM mat_records m "
    "CROSS JOIN LATERAL unnest(m.search) AS w "
    "WHERE m.domain_id = :d AND m.branch = :b AND m.search IS NOT NULL AND NOT m.deleted "
    "AND lb_lexicon_word(w.lexeme) GROUP BY m.kind_id, w.lexeme"
)


async def lexicon(session: AsyncSession, branch: str, domain_id: str = DOMAIN) -> dict[str, int]:
    """The word lexicon of a branch's own rows, as the triggers of revision 0018 keep it:
    rows holding each word, all Kinds together."""
    found: dict[str, int] = {}
    for _, word, records in (await session.execute(_LEXICON, {"d": domain_id, "b": branch})).all():
        found[word] = found.get(word, 0) + int(records)
    return found


async def assert_lexicon_in_step(
    session: AsyncSession, branch: str, domain_id: str = DOMAIN
) -> None:
    """The kept lexicon equals one recomputed from the branch's own search vectors, per Kind."""
    params = {"d": domain_id, "b": branch}
    kept = {(k, w): int(n) for k, w, n in (await session.execute(_LEXICON, params)).all()}
    rows = (await session.execute(_LEXICON_RECOMPUTED, params)).all()
    assert kept == {(k, w): int(n) for k, w, n in rows}, branch
