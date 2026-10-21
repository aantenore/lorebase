"""Reviewer edits of extraction items, validated against the schema."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from lorebase_core.model import Schema
from lorebase_core.model.yaml_schema import load_schema
from lorebase_store.extraction import ItemEditError, effective_subject, normalize_edits
from lorebase_store.repositories import ItemRow

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "supply" / "schema.yaml"


@pytest.fixture(scope="module")
def schema() -> Schema:
    return load_schema(EXAMPLE.read_text())


def item(schema: Schema, item_type: str, **subject: object) -> ItemRow:
    return ItemRow(
        item_id="pit_1",
        item_type=item_type,
        seq=0,
        subject=dict(subject),
        partition="p",
        element_key="k",
    )


def test_record_edits_are_encoded_by_attribute(schema: Schema) -> None:
    contract = schema.kind_by_api_name("Contract")
    assert contract is not None
    start = contract.attribute_by_api_name("start_date")
    value = contract.attribute_by_api_name("annual_value")
    assert start is not None
    assert value is not None
    row = item(schema, "record", kind=contract.id, key="C-3025", values={start.id: "2024-01-01"})
    edits = normalize_edits(row, {"values": {"start_date": "2024-02-08", value.id: 10}}, schema)
    assert edits == {"values": {start.id: "2024-02-08", value.id: "10.00"}}
    applied = effective_subject(replace(row, edits=edits))
    assert applied["values"] == {start.id: "2024-02-08", value.id: "10.00"}


def test_invalid_edits_are_refused(schema: Schema) -> None:
    contract = schema.kind_by_api_name("Contract")
    part = schema.kind_by_api_name("Part")
    assert contract is not None
    assert part is not None
    record = item(schema, "record", kind=contract.id, key="C-3025", values={})
    with pytest.raises(ItemEditError, match="unknown Attribute"):
        normalize_edits(record, {"values": {"nope": 1}}, schema)
    with pytest.raises(ItemEditError, match="start_date"):
        normalize_edits(record, {"values": {"start_date": "soon"}}, schema)
    with pytest.raises(ItemEditError, match="accept edits"):
        normalize_edits(record, {"key": "C-1"}, schema)
    count = part.attribute_by_api_name("supplier_count")
    assert count is not None
    with pytest.raises(ItemEditError, match="derived"):
        normalize_edits(
            item(schema, "record", kind=part.id, key="P-1", values={}),
            {"values": {"supplier_count": 2}},
            schema,
        )


def test_value_and_link_edits_pick_other_records(schema: Schema) -> None:
    supplier = schema.kind_by_api_name("Supplier")
    found = schema.relation_by_api_name("supplied_by")
    assert supplier is not None
    assert found is not None
    score = supplier.attribute_by_api_name("risk_score")
    assert score is not None
    value = item(schema, "value", kind=supplier.id, key="S-1", attribute=score.id, value="10")
    assert normalize_edits(value, {"value": "74", "key": " S-1025 "}, schema) == {
        "value": "74",
        "key": "S-1025",
    }
    link = item(schema, "link", relation=found[0].id, **{"from": "P-1", "to": "S-1"})
    edits = normalize_edits(link, {"to": "S-1025", "values": {"is_primary": True}}, schema)
    assert edits["to"] == "S-1025"
    applied = effective_subject(replace(link, edits=edits))
    assert (applied["from"], applied["to"]) == ("P-1", "S-1025")
    assert normalize_edits(link, None, schema) == {}
