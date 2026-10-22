"""Row mapping: transforms, coercion through the core encoding, keys and Link targets."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from lorebase_core.model import Schema, Source
from lorebase_core.model.source import AttributeMapping, RelationMapping, Transform
from lorebase_core.types import AttributeType, TypeSpec
from lorebase_scribe.sources.mapping import (
    RowError,
    RowMapper,
    SourceDefinitionError,
    apply_transform,
    coerce,
)

SUPPLIER_ROW = {
    "supplier_code": " S-1001 ",
    "legal_name": "  Kavsen Alloys Sp. z o.o. ",
    "country": "pl",
    "city": "Poznan",
    "tier": "1",
    "risk_score": "32",
    "status": "active",
    "onboarded_on": "2024-02-26",
    "vat_number": "PL000007919",
}


def source(schema: Schema, name: str) -> Source:
    found = schema.source_by_api_name(name)
    assert found is not None
    return found


def attribute_id(schema: Schema, kind: str, name: str) -> str:
    found = schema.kind_by_api_name(kind)
    assert found is not None
    attribute = found.attribute_by_api_name(name)
    assert attribute is not None
    return attribute.id


def test_supplier_row_maps_to_canonical_values(supply_schema: Schema) -> None:
    mapper = RowMapper(supply_schema, source(supply_schema, "approved.suppliers"))
    mapped = mapper.map(SUPPLIER_ROW, "suppliers.csv:2")
    a = lambda name: attribute_id(supply_schema, "Supplier", name)  # noqa: E731
    assert mapped.key == "S-1001"
    assert mapped.locator == "suppliers.csv:2"
    assert mapped.values == {
        a("supplier_code"): "S-1001",
        a("legal_name"): "Kavsen Alloys Sp. z o.o.",
        a("country"): "PL",
        a("city"): "Poznan",
        a("tier"): "1",
        a("risk_score"): "32",
        a("onboarded_on"): "2024-02-26",
    }
    # Unmapped Attributes (status, vat_number) are not owned by the Source.
    assert a("status") not in mapper.owned_attributes
    assert a("supplier_code") in mapper.owned_attributes


def test_record_keeps_values_the_source_does_not_own(supply_schema: Schema) -> None:
    mapper = RowMapper(supply_schema, source(supply_schema, "approved.suppliers"))
    mapped = mapper.map({**SUPPLIER_ROW, "city": ""}, "row")
    status = attribute_id(supply_schema, "Supplier", "status")
    city = attribute_id(supply_schema, "Supplier", "city")
    record = mapper.record(mapped, {status: "on_hold", city: "Gdansk"})
    assert record.values[status] == "on_hold"  # kept: not mapped
    assert city not in record.values  # mapped and now empty: removed


def test_bad_values_are_row_errors(supply_schema: Schema) -> None:
    mapper = RowMapper(supply_schema, source(supply_schema, "approved.suppliers"))
    with pytest.raises(RowError, match="risk_score") as caught:
        mapper.map({**SUPPLIER_ROW, "risk_score": "high"}, "f.csv:7")
    assert caught.value.as_dict()["row"] == "f.csv:7"
    with pytest.raises(RowError, match="key column"):
        mapper.map({**SUPPLIER_ROW, "supplier_code": " "}, "f.csv:8")
    with pytest.raises(RowError, match="tier"):
        mapper.map({**SUPPLIER_ROW, "tier": "7"}, "f.csv:9")  # not an enum member


def test_purchase_order_links_and_native_values(supply_schema: Schema) -> None:
    mapper = RowMapper(supply_schema, source(supply_schema, "erp.purchase_orders"))
    row: dict[str, Any] = {
        "po_no": "PO-200001",
        "vendor_id": "S-1005",
        "state": "SHIPPED",
        "ordered": date(2025, 11, 16),
        "due": datetime(2026, 1, 10, 0, 0),
        "qty": 2500,
        "price": Decimal("11.62"),
        "currency": "eur",
        "part_no": "P-10001",
        "plant_code": None,
        "deleted_at": None,
    }
    mapped = mapper.map(row, "purchase_orders:1")
    a = lambda name: attribute_id(supply_schema, "PurchaseOrder", name)  # noqa: E731
    assert mapped.values[a("status")] == "shipped"
    assert mapped.values[a("ordered_on")] == "2025-11-16"
    assert mapped.values[a("due_on")] == "2026-01-10"
    assert mapped.values[a("quantity")] == "2500"
    assert mapped.values[a("unit_price")] == "11.62"
    assert mapped.values[a("currency")] == "EUR"
    assert mapped.values[a("supplier")] == "S-1005"
    for_part = supply_schema.relation_by_api_name("for_part")
    delivered_to = supply_schema.relation_by_api_name("delivered_to")
    assert for_part is not None
    assert delivered_to is not None
    assert mapped.links == {
        for_part[0].id: frozenset({"P-10001"}),
        delivered_to[0].id: frozenset(),
    }
    tombstone = mapper.map({**row, "deleted_at": datetime(2026, 1, 1, tzinfo=UTC)}, "x")
    assert tombstone.tombstone
    assert tombstone.key == "PO-200001"


def test_missing_columns_are_reported(supply_schema: Schema) -> None:
    mapper = RowMapper(supply_schema, source(supply_schema, "approved.suppliers"))
    with pytest.raises(SourceDefinitionError, match="risk_score"):
        mapper.check_columns([c for c in SUPPLIER_ROW if c != "risk_score"])


def test_expression_mappings_start_from_the_row(supply_schema: Schema) -> None:
    suppliers = source(supply_schema, "approved.suppliers")
    legal_name = attribute_id(supply_schema, "Supplier", "legal_name")
    city = attribute_id(supply_schema, "Supplier", "city")
    mapping = dict(suppliers.attribute_mapping)
    mapping[legal_name] = AttributeMapping(
        expression='legal_name.trim() + " (" + country.upper() + ")"',
        transforms=(Transform(name="upper"),),
    )
    mapping[city] = AttributeMapping(expression='row["city"]')
    mapper = RowMapper(supply_schema, suppliers.model_copy(update={"attribute_mapping": mapping}))
    mapped = mapper.map(SUPPLIER_ROW, "suppliers.csv:2")
    assert mapped.values[legal_name] == "KAVSEN ALLOYS SP. Z O.O. (PL)"
    assert mapped.values[city] == "Poznan"
    # Columns an expression reads are not required up front: the row decides.
    assert "legal_name" not in mapper.columns
    broken = {**mapping, city: AttributeMapping(expression="town")}
    failing = RowMapper(supply_schema, suppliers.model_copy(update={"attribute_mapping": broken}))
    with pytest.raises(RowError, match="city"):
        failing.map(SUPPLIER_ROW, "suppliers.csv:2")


def test_definition_errors(supply_schema: Schema) -> None:
    suppliers = source(supply_schema, "approved.suppliers")
    legal_name = attribute_id(supply_schema, "Supplier", "legal_name")
    with pytest.raises(SourceDefinitionError, match="cast needs"):
        RowMapper(
            supply_schema,
            suppliers.model_copy(
                update={
                    "attribute_mapping": {
                        legal_name: AttributeMapping(
                            column="legal_name",
                            transforms=(Transform(name="cast", args={"to": "money"}),),
                        )
                    }
                }
            ),
        )
    issued_to = supply_schema.relation_by_api_name("issued_to")
    assert issued_to is not None
    orders = source(supply_schema, "erp.purchase_orders")
    with pytest.raises(SourceDefinitionError, match="derives its Links"):
        RowMapper(
            supply_schema,
            orders.model_copy(
                update={
                    "relation_mapping": (
                        RelationMapping(relation=issued_to[0].id, columns=("vendor_id",)),
                    )
                }
            ),
        )


@pytest.mark.parametrize(
    ("transform", "value", "row", "expected"),
    [
        (Transform(name="trim"), "  a ", {}, "a"),
        (Transform(name="lower"), "AbC", {}, "abc"),
        (Transform(name="upper"), "abc", {}, "ABC"),
        (Transform(name="null_if", args={"value": "n/a"}), "n/a", {}, None),
        (Transform(name="null_if", args={"values": ["-", ""]}), "x", {}, "x"),
        (Transform(name="constant", args={"value": "EUR"}), "ignored", {}, "EUR"),
        (
            Transform(name="concat", args={"columns": ["b", "c"], "separator": " "}),
            "a",
            {"b": "b", "c": None},
            "a b",
        ),
        (Transform(name="map", args={"values": {"A": "active"}}), "A", {}, "active"),
        (Transform(name="map", args={"values": {"A": "active"}, "default": "x"}), "B", {}, "x"),
        (
            Transform(name="parse_date", args={"format": "%d/%m/%Y"}),
            "26/02/2024",
            {},
            date(2024, 2, 26),
        ),
        (Transform(name="cast", args={"to": "integer"}), "42", {}, 42),
        (Transform(name="cast", args={"to": "integer"}), Decimal("42.0"), {}, 42),
        (Transform(name="cast", args={"to": "decimal"}), "1.50", {}, Decimal("1.50")),
        (Transform(name="cast", args={"to": "boolean"}), "Yes", {}, True),
        (Transform(name="cast", args={"to": "string"}), 7, {}, "7"),
    ],
)
def test_transforms(transform: Transform, value: Any, row: dict[str, Any], expected: Any) -> None:
    assert apply_transform(transform, value, row) == expected


@pytest.mark.parametrize(
    ("value", "spec", "expected"),
    [
        ("", TypeSpec.of(AttributeType.STRING), None),
        ("true", TypeSpec.of(AttributeType.BOOLEAN), True),
        (True, TypeSpec.of(AttributeType.BOOLEAN), True),
        (" 12 ", TypeSpec.of(AttributeType.INTEGER), "12"),
        (12, TypeSpec.of(AttributeType.STRING), "12"),
        (1.25, TypeSpec.decimal(10, 2), "1.25"),
        ('{"a": 1}', TypeSpec.of(AttributeType.JSON), {"a": 1}),
        (
            datetime(2026, 1, 1, 8, 30),
            TypeSpec.of(AttributeType.TIMESTAMP),
            "2026-01-01T08:30:00.000000Z",
        ),
    ],
)
def test_coercion(value: Any, spec: TypeSpec, expected: Any) -> None:
    assert coerce(value, spec) == expected
