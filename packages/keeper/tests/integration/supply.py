"""Load the `supply` example (schema and CSV rows) through the store commit pipeline."""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Attribute, Kind, Link, Record, Relation, Schema
from lorebase_core.model.yaml_schema import load_schema
from lorebase_core.revision import Author, Origin
from lorebase_core.types import AttributeType, encode_value
from lorebase_core.validate import DataChange, LinkUpsert, RecordUpsert
from lorebase_store.commit import CommitResult, SchemaUpsert, commit, create_domain
from lorebase_store.runtime import StoreRuntime

EXAMPLE = Path(__file__).resolve().parents[4] / "examples" / "supply"
DOMAIN = "supply"
AUTHOR = Author(principal="usr_loader")

KIND_FILES = {
    "Supplier": "supplier.csv",
    "Part": "part.csv",
    "Plant": "plant.csv",
    "Contract": "contract.csv",
    "PurchaseOrder": "purchase_order.csv",
    "RiskFlag": "risk_flag.csv",
}
LINK_FILES = {
    "supplied_by": "supplied_by.csv",
    "stocked_at": "stocked_at.csv",
    "for_part": "for_part.csv",
    "delivered_to": "delivered_to.csv",
    "with_supplier": "with_supplier.csv",
    "flags": "flags.csv",
}


class Clock:
    """Strictly increasing deterministic clock."""

    def __init__(self) -> None:
        self._now = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now


def supply_schema() -> Schema:
    """The example schema, CEL Operations and Derivations included."""
    return load_schema((EXAMPLE / "schema.yaml").read_text())


def encode(attribute: Attribute, raw: str) -> Any:
    if attribute.type.name is AttributeType.BOOLEAN:
        return raw.strip().lower() == "true"
    return encode_value(attribute.type, raw)


def rows(name: str) -> list[dict[str, str]]:
    with (EXAMPLE / "data" / name).open(newline="") as handle:
        return list(csv.DictReader(handle))


def record_change(kind: Kind, row: dict[str, str], key: str | None = None) -> RecordUpsert:
    values: dict[str, Any] = {}
    for name, raw in row.items():
        attribute = kind.attribute_by_api_name(name)
        if attribute is not None and raw != "":
            values[attribute.id] = encode(attribute, raw)
    if key is None:
        key = str(values[kind.primary_key[0]]) if kind.primary_key else row["key"]
    return RecordUpsert(record=Record(kind=kind.id, key=key, values=values))


def link_change(relation: Relation, row: dict[str, str]) -> LinkUpsert:
    values: dict[str, Any] = {}
    for name, raw in row.items():
        attribute = next((a for a in relation.attributes.values() if a.api_name == name), None)
        if attribute is not None and raw != "":
            values[attribute.id] = encode(attribute, raw)
    return LinkUpsert(
        link=Link(
            relation=relation.id, from_key=row["from_key"], to_key=row["to_key"], values=values
        )
    )


def example_changes(schema: Schema) -> list[DataChange]:
    changes: list[DataChange] = []
    for kind_name, file in KIND_FILES.items():
        kind = schema.kind_by_api_name(kind_name)
        assert kind is not None
        changes.extend(record_change(kind, row) for row in rows(file))
    for relation_name, file in LINK_FILES.items():
        found = schema.relation_by_api_name(relation_name)
        assert found is not None
        changes.extend(link_change(found[0], row) for row in rows(file))
    return changes


async def load_supply(
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, schema: Schema | None = None
) -> tuple[Schema, list[CommitResult]]:
    """Create the Domain, commit the schema (the example's unless given), then the example
    rows; returns the commits."""
    schema = schema or supply_schema()
    created = await create_domain(
        session, schema.domain, author=AUTHOR, runtime=runtime, clock=clock
    )
    schema_commit = await commit(
        session,
        DOMAIN,
        "main",
        [],
        schema_changes=[SchemaUpsert(e) for e in schema.elements()],  # type: ignore[arg-type]
        author=AUTHOR,
        message="Load the supply schema",
        origin=Origin(type="import"),
        runtime=runtime,
        clock=clock,
    )
    data_commit = await commit(
        session,
        DOMAIN,
        "main",
        example_changes(schema),
        author=AUTHOR,
        message="Import the supply example data",
        origin=Origin(type="import", ref="examples/supply"),
        runtime=runtime,
        clock=clock,
    )
    return schema, [created, schema_commit, data_commit]


async def commit_changes(
    session: AsyncSession,
    runtime: StoreRuntime,
    clock: Clock,
    changes: Iterable[DataChange],
    *,
    message: str,
    branch: str = "main",
    origin: str = "operation",
    author: str = "usr_editor",
) -> CommitResult:
    return await commit(
        session,
        DOMAIN,
        branch,
        list(changes),
        author=Author(principal=author),
        message=message,
        origin=Origin(type=origin),
        runtime=runtime,
        clock=clock,
    )


def synthetic_parts(schema: Schema, count: int, *, start: int = 50_000) -> Sequence[RecordUpsert]:
    """`count` extra Parts with deterministic values, for the timing tests."""
    kind = schema.kind_by_api_name("Part")
    assert kind is not None
    categories = ["bearing", "fastener", "seal", "gasket", "valve", "filter", "sensor"]
    return [
        record_change(
            kind,
            {
                "part_number": f"P-{start + i}",
                "name": f"Synthetic {categories[i % len(categories)]} {i}",
                "category": categories[i % len(categories)],
                "summary": f"Generated part number {i} for paging and search timings.",
                "unit_cost": f"{(i % 997) / 7:.2f}",
                "lifecycle": "active",
            },
        )
        for i in range(count)
    ]
