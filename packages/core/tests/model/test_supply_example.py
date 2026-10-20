"""The demo Domain under ``examples/supply``: schema, data, generator and documents."""

from __future__ import annotations

import csv
import importlib.util
import re
import shutil
import sys
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from lorebase_core.model import (
    Attribute,
    Kind,
    Link,
    Record,
    Relation,
    Schema,
    dump_schema,
    load_schema,
    schema_to_document,
    validate_schema,
)
from lorebase_core.types import AttributeType, encode_value
from lorebase_core.validate import (
    DataChange,
    IssueCode,
    LinkUpsert,
    RecordUpsert,
    SchemaRules,
    validate_changeset,
    validate_record,
)

from .yaml_support import counting_factory, strip_ids

EXAMPLE = Path(__file__).parents[4] / "examples" / "supply"
SCRIPT = EXAMPLE / "scripts" / "make_supply_data.py"

#: CSV file stem -> Kind api name, and Relation api name (explicit Relations only).
RECORD_FILES = {
    "supplier": "Supplier",
    "part": "Part",
    "plant": "Plant",
    "purchase_order": "PurchaseOrder",
    "contract": "Contract",
    "risk_flag": "RiskFlag",
}
LINK_FILES = ("supplied_by", "stocked_at", "for_part", "delivered_to", "with_supplier", "flags")


@pytest.fixture(scope="module")
def schema() -> Schema:
    return load_schema((EXAMPLE / "schema.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_supply_data", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# Schema -----------------------------------------------------------------------------------------


def test_schema_is_valid_with_expressions_enabled(schema: Schema) -> None:
    assert validate_schema(schema, SchemaRules(allow_expressions=True)) == []


def test_only_the_expression_gate_blocks_the_default_rules(schema: Schema) -> None:
    issues = validate_schema(schema)
    assert {issue.code for issue in issues} == {IssueCode.SCHEMA_EXPRESSIONS_DISABLED}
    assert {issue.path[0] for issue in issues} == {"operations", "derivations"}


def test_schema_has_the_required_elements(schema: Schema) -> None:
    kinds = {kind.api_name for kind in schema.kinds.values()}
    assert {"Supplier", "Part", "PurchaseOrder", "Plant", "Contract"} <= kinds
    assert len(schema.traits) >= 1
    assert len(schema.operations) == 2
    assert len(schema.derivations) >= 1
    assert {source.type.value for source in schema.sources.values()} == {"csv", "postgres_table"}
    assert len({relation.cardinality for relation in schema.relations.values()}) >= 2


def test_reload_against_itself_changes_nothing(schema: Schema) -> None:
    document = strip_ids(schema_to_document(schema))
    text = yaml.safe_dump(document, sort_keys=False)
    assert load_schema(text, base=schema, id_factory=counting_factory()) == schema


def test_dump_and_reload_is_stable(schema: Schema) -> None:
    assert load_schema(dump_schema(schema)) == schema


def test_committed_ids_are_written_in_the_file(schema: Schema) -> None:
    """The example is its own fixture: ids in the file are the ids of the loaded schema."""
    text = (EXAMPLE / "schema.yaml").read_text(encoding="utf-8")
    for element in schema.elements():
        assert element.id in text


# Data -------------------------------------------------------------------------------------------


def read_csv(directory: Path, stem: str) -> list[dict[str, str]]:
    with (directory / f"{stem}.csv").open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def typed(attribute: Attribute, text: str) -> Any:
    """CSV text -> canonical value of the Attribute type (empty cells are absent)."""
    if text == "":
        return None
    if attribute.type.name is AttributeType.BOOLEAN:
        assert text in ("true", "false")
        return text == "true"
    return encode_value(attribute.type, text)


def build_changes(schema: Schema, directory: Path) -> tuple[list[DataChange], DataIndex]:
    changes: list[DataChange] = []
    records: dict[tuple[str, str], Record] = {}
    links: dict[str, list[tuple[str, str]]] = {}
    for stem, kind_name in RECORD_FILES.items():
        kind = schema.kind_by_api_name(kind_name)
        assert kind is not None
        for row in read_csv(directory, stem):
            values: dict[str, Any] = {}
            for column, text in row.items():
                if column == "key":
                    continue
                attribute = kind.attribute_by_api_name(column)
                assert attribute is not None, f"{stem}.csv: unknown column {column}"
                value = typed(attribute, text)
                if value is not None:
                    values[attribute.id] = value
            key = row["key"] if "key" in row else primary_key_text(kind, row)
            record = Record(kind=kind.id, key=key, values=values)
            assert (kind.id, key) not in records, f"{stem}.csv: duplicate key {key}"
            records[(kind.id, key)] = record
            changes.append(RecordUpsert(record=record))
    for stem in LINK_FILES:
        found = schema.relation_by_api_name(stem)
        assert found is not None
        relation: Relation = found[0]
        for row in read_csv(directory, stem):
            values = {}
            for column, text in row.items():
                if column in ("from_key", "to_key"):
                    continue
                attribute = next(
                    (a for a in relation.attributes.values() if a.api_name == column), None
                )
                assert attribute is not None, f"{stem}.csv: unknown column {column}"
                value = typed(attribute, text)
                if value is not None:
                    values[attribute.id] = value
            link = Link.model_validate(
                {
                    "relation": relation.id,
                    "from": row["from_key"],
                    "to": row["to_key"],
                    "values": values,
                }
            )
            links.setdefault(relation.id, []).append((link.from_key, link.to_key))
            changes.append(LinkUpsert(link=link))
    return changes, DataIndex(records, links)


def primary_key_text(kind: Kind, row: dict[str, str]) -> str:
    return row[kind.attributes[kind.primary_key[0]].api_name]


class DataIndex:
    """In-memory index over an empty store: lookups see only the batch under test."""

    def __init__(self, records: dict[tuple[str, str], Record], links: dict[str, Any]) -> None:
        self.records = records
        self.links = links

    def record_exists(self, kind_id: str, key: str) -> bool:
        return False

    def find_unique(self, kind_id: str, attribute_id: str, value: Any) -> str | None:
        return None

    def keys_referencing(self, kind_id: str, attribute_id: str, key: str) -> Iterable[str]:
        return ()

    def link_exists(self, relation_id: str, from_key: str, to_key: str) -> bool:
        return False

    def count_links(
        self, relation_id: str, *, from_key: str | None = None, to_key: str | None = None
    ) -> int:
        return 0

    def links_of(
        self, relation_id: str, *, from_key: str | None = None, to_key: str | None = None
    ) -> Iterable[tuple[str, str]]:
        return ()


def check_dataset(schema: Schema, directory: Path) -> None:
    changes, index = build_changes(schema, directory)
    for change in changes:
        if isinstance(change, RecordUpsert):
            assert validate_record(schema, change.record) == []
    issues = validate_changeset(schema, index, changes)
    assert [(issue.code.value, issue.message) for issue in issues] == []
    check_foreign_keys(schema, index)


def check_foreign_keys(schema: Schema, index: DataIndex) -> None:
    """Every ``ref`` value and every Link endpoint names a Record of the dataset."""
    keys = set(index.records)
    for record in index.records.values():
        kind = schema.kinds[record.kind]
        for attribute_id, value in record.values.items():
            spec = kind.attributes[attribute_id].type
            if spec.kind_ref is not None:
                assert (spec.kind_ref, value) in keys, f"{record.key}: dangling {value}"
    for relation_id, pairs in index.links.items():
        relation = schema.relations[relation_id]
        for from_key, to_key in pairs:
            assert (relation.from_kind, from_key) in keys, f"{relation.api_name}: {from_key}"
            assert (relation.to_kind, to_key) in keys, f"{relation.api_name}: {to_key}"


def test_committed_data_is_valid_and_consistent(schema: Schema) -> None:
    check_dataset(schema, EXAMPLE / "data")


@pytest.mark.parametrize(
    ("stem", "extra_row"),
    [
        ("supplied_by", "P-99999,S-1001,false,10"),
        ("for_part", "PO-200001,P-10002"),
        ("purchase_order", "PO-200001,S-1001,issued,2025-12-01,2026-01-10,10,1.00,EUR"),
        ("supplier", "S-1001,Duplicate Co,IT,Turin,2,10,active,2020-01-01,IT000000001"),
    ],
)
def test_dataset_check_catches_broken_data(
    schema: Schema, tmp_path: Path, stem: str, extra_row: str
) -> None:
    shutil.copytree(EXAMPLE / "data", tmp_path / "data")
    with (tmp_path / "data" / f"{stem}.csv").open("a", encoding="utf-8") as handle:
        handle.write(extra_row + "\n")
    with pytest.raises(AssertionError):
        check_dataset(schema, tmp_path / "data")


def test_committed_files_have_twenty_to_fifty_rows() -> None:
    for stem in (*RECORD_FILES, *LINK_FILES):
        count = len(read_csv(EXAMPLE / "data", stem))
        assert 20 <= count <= 50, f"{stem}.csv has {count} rows"


def test_committed_data_tells_the_story(schema: Schema) -> None:
    data = EXAMPLE / "data"
    suppliers_per_part: dict[str, int] = {}
    for row in read_csv(data, "supplied_by"):
        suppliers_per_part[row["from_key"]] = suppliers_per_part.get(row["from_key"], 0) + 1
    assert set(suppliers_per_part) == {row["part_number"] for row in read_csv(data, "part")}
    assert sum(1 for count in suppliers_per_part.values() if count == 1) >= 10
    as_of = date(2026, 1, 15)
    expiring = [
        row
        for row in read_csv(data, "contract")
        if row["status"] == "active"
        and 0 <= (date.fromisoformat(row["end_date"]) - as_of).days <= 90
    ]
    assert len(expiring) >= 3


# Generator --------------------------------------------------------------------------------------


def test_default_generator_output_is_the_committed_data(generator: Any, tmp_path: Path) -> None:
    settings = generator.Settings(
        1, generator.DEFAULT_SEED, date.fromisoformat(generator.DEFAULT_AS_OF)
    )
    generator.write_dataset(generator.build_dataset(settings), tmp_path)
    for path in sorted(tmp_path.glob("*.csv")):
        assert path.read_bytes() == (EXAMPLE / "data" / path.name).read_bytes(), path.name
    assert {p.name for p in tmp_path.glob("*.csv")} == {
        p.name for p in (EXAMPLE / "data").glob("*.csv")
    }


def test_generator_is_deterministic_and_scales(
    generator: Any, schema: Schema, tmp_path: Path
) -> None:
    as_of = date.fromisoformat(generator.DEFAULT_AS_OF)
    first = generator.build_dataset(generator.Settings(3, 7, as_of))
    assert first == generator.build_dataset(generator.Settings(3, 7, as_of))
    assert first != generator.build_dataset(generator.Settings(3, 8, as_of))
    assert len(first["supplier"]) == 3 * generator.BASE_COUNTS["suppliers"]
    generator.write_dataset(first, tmp_path)
    check_dataset(schema, tmp_path)


def test_generator_cli_writes_files(
    generator: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    generator.main(["--scale", "1", "--out", str(tmp_path)])
    assert "supplier.csv: 30 rows" in capsys.readouterr().out
    assert (tmp_path / "flags.csv").exists()
    with pytest.raises(SystemExit):
        generator.main(["--scale", "0", "--out", str(tmp_path)])


# Documents --------------------------------------------------------------------------------------


def test_documents_mention_real_entities() -> None:
    documents = sorted((EXAMPLE / "documents").glob("*.md"))
    assert 3 <= len(documents) <= 4
    data = EXAMPLE / "data"
    known = {
        "S": {row["supplier_code"] for row in read_csv(data, "supplier")},
        "P": {row["part_number"] for row in read_csv(data, "part")},
        "C": {row["contract_number"] for row in read_csv(data, "contract")},
        "PO": {row["po_number"] for row in read_csv(data, "purchase_order")},
        "PL": {row["plant_code"] for row in read_csv(data, "plant")},
    }
    names = {row["legal_name"] for row in read_csv(data, "supplier")}
    for document in documents:
        text = document.read_text(encoding="utf-8")
        for prefix, keys in known.items():
            for match in re.findall(rf"\b{prefix}-\d+\b", text):
                assert match in keys, f"{document.name}: unknown {match}"
        assert any(name in text for name in names), document.name
