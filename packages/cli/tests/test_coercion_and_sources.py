"""Type coercion against the schema, and the readers of import files."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from lorebase_cli import importing, sources
from lorebase_cli.coercion import (
    CoercionError,
    attribute_types,
    coerce_row,
    coerce_value,
    is_missing,
)
from lorebase_cli.failures import CliError

SCHEMA: dict[str, Any] = {
    "kinds": {
        "k1": {
            "api_name": "Part",
            "primary_key": ["a1"],
            "attributes": {
                "a1": {"api_name": "part_number", "type": {"name": "string"}},
                "a2": {"api_name": "unit_cost", "type": {"name": "decimal"}},
                "a3": {"api_name": "hazardous", "type": {"name": "boolean"}},
                "a4": {"api_name": "stock", "type": {"name": "integer"}},
            },
        },
        "k2": {
            "api_name": "RiskFlag",
            "primary_key": [],
            "attributes": {"a5": {"api_name": "reason", "type": {"name": "text"}}},
        },
    },
    "relations": {
        "r1": {
            "api_name": "supplied_by",
            "attributes": {
                "a6": {"api_name": "is_primary", "type": {"name": "boolean"}},
                "a7": {"api_name": "lead_time_days", "type": {"name": "integer"}},
            },
        }
    },
}


@pytest.mark.parametrize(
    ("type_name", "raw", "expected"),
    [
        ("boolean", "true", True),
        ("boolean", "FALSE", False),
        ("boolean", "yes", True),
        ("boolean", "0", False),
        ("boolean", True, True),
        ("boolean", 1, True),
        ("integer", "32", "32"),
        ("integer", " -7 ", "-7"),
        ("integer", 12, "12"),
        ("integer", 12.0, "12"),
        ("decimal", "10.79", "10.79"),
        ("decimal", Decimal("1E+2"), "100"),
        ("decimal", 0.1, "0.1"),
        ("decimal", 5, "5"),
        ("float", "1.5", 1.5),
        ("float", 2, 2.0),
        ("string", 42, "42"),
        ("enum", "active", "active"),
        ("date", date(2024, 2, 26), "2024-02-26"),
        ("date", "2024-02-26", "2024-02-26"),
        ("timestamp", datetime(2025, 7, 3, 2, 38, tzinfo=UTC), "2025-07-03T02:38:00+00:00"),
        ("timestamp", "2025-07-03T02:38:11.000000Z", "2025-07-03T02:38:11.000000Z"),
        ("json", '{"a": [1, 2]}', {"a": [1, 2]}),
        ("json", {"a": 1}, {"a": 1}),
        ("array", "[1, 2]", [1, 2]),
        ("geo_point", '{"lat": 1, "lon": 2}', {"lat": 1, "lon": 2}),
    ],
)
def test_values_take_the_form_keeper_expects(type_name: str, raw: Any, expected: Any) -> None:
    assert coerce_value(type_name, "c", raw) == expected


@pytest.mark.parametrize(
    ("type_name", "raw"),
    [
        ("boolean", "maybe"),
        ("boolean", 2),
        ("integer", "1.5"),
        ("integer", "abc"),
        ("integer", True),
        ("decimal", "ten"),
        ("decimal", "NaN"),
        ("float", "x"),
        ("float", "inf"),
        ("json", "{not json"),
        ("array", '{"a": 1}'),
        ("geo_point", "[1, 2]"),
    ],
)
def test_values_that_do_not_fit_name_their_column(type_name: str, raw: Any) -> None:
    with pytest.raises(CoercionError, match="column 'c'"):
        coerce_value(type_name, "c", raw)


def test_decimals_never_pass_through_floats() -> None:
    assert coerce_value("decimal", "c", "0.30000000000000004") == "0.30000000000000004"
    assert coerce_value("integer", "c", "9007199254740993") == "9007199254740993"


def test_missing_values_are_unset() -> None:
    assert all(is_missing(v) for v in ("", "  ", None, float("nan")))
    assert not any(is_missing(v) for v in ("0", 0, False, "x"))


def test_rows_are_coerced_renamed_and_checked() -> None:
    types = attribute_types(SCHEMA, "kinds", "Part")
    row = {"part_number": "P-1", "unit_cost": "1.50", "hazardous": "true", "stock": "", "x": "y"}
    assert coerce_row(types, row, ignore_unknown=True) == {
        "part_number": "P-1",
        "unit_cost": "1.50",
        "hazardous": True,
    }
    assert coerce_row(types, {"PN": "P-2"}, renames={"PN": "part_number"}) == {"part_number": "P-2"}
    with pytest.raises(CoercionError, match=r"'x'.*not an Attribute"):
        coerce_row(types, row)
    assert coerce_row(types, {"key": 5}, extra_columns=("key",)) == {"key": "5"}


def test_schema_lookup_names_what_exists() -> None:
    assert attribute_types(SCHEMA, "relations", "supplied_by") == {
        "is_primary": "boolean",
        "lead_time_days": "integer",
    }
    with pytest.raises(CliError, match=r"unknown Kind 'Nope'.*Part, RiskFlag"):
        attribute_types(SCHEMA, "kinds", "Nope")
    with pytest.raises(CliError, match="unknown Relation"):
        attribute_types(SCHEMA, "relations", "nope")


def test_link_rows_map_the_csv_ends_and_kinds_without_a_key_accept_one() -> None:
    links = importing.prepare(SCHEMA, "links", "supplied_by")
    rows = list(
        importing.coerced_rows(
            [{"from_key": "P-1", "to_key": "S-1", "is_primary": "true", "lead_time_days": "4"}],
            links,
            ignore_unknown=False,
        )
    )
    assert rows == [{"from": "P-1", "to": "S-1", "is_primary": True, "lead_time_days": "4"}]
    keyed = importing.prepare(SCHEMA, "records", "RiskFlag")
    assert keyed.extra_columns == ("key",)
    assert importing.prepare(SCHEMA, "records", "Part").extra_columns == ()


def test_a_bad_value_names_its_row() -> None:
    prepared = importing.prepare(SCHEMA, "records", "Part")
    rows = [{"part_number": "P-1"}, {"part_number": "P-2", "stock": "many"}]
    with pytest.raises(CliError, match=r"row 2: column 'stock'"):
        list(importing.coerced_rows(rows, prepared, ignore_unknown=False))


def test_map_options_parse() -> None:
    assert importing.parse_renames(["a=b", "c=d"]) == {"a": "b", "c": "d"}
    with pytest.raises(CliError, match="column=attribute"):
        importing.parse_renames(["nonsense"])


# Sources ------------------------------------------------------------------------------------


def test_format_follows_the_suffix_unless_given() -> None:
    assert sources.detect_format("a.csv", None) == "csv"
    assert sources.detect_format("a.NDJSON", None) == "jsonl"
    assert sources.detect_format("a.parquet", None) == "parquet"
    assert sources.detect_format("data.txt", "tsv") == "tsv"
    with pytest.raises(CliError, match="--format"):
        sources.detect_format("data.txt", None)
    with pytest.raises(CliError, match="unknown format"):
        sources.detect_format("a.csv", "xml")


def test_csv_tsv_and_bom(tmp_path: Path) -> None:
    csv_file = tmp_path / "a.csv"
    csv_file.write_text("﻿a,b\n1,x y\n2,\n", encoding="utf-8")
    assert list(sources.read_rows(str(csv_file), "csv")) == [
        {"a": "1", "b": "x y"},
        {"a": "2", "b": ""},
    ]
    tsv = tmp_path / "a.tsv"
    tsv.write_text("a\tb\n1\t2\n")
    assert list(sources.read_rows(str(tsv), "tsv")) == [{"a": "1", "b": "2"}]
    semi = tmp_path / "s.csv"
    semi.write_text("a;b\n1;2\n")
    assert list(sources.read_rows(str(semi), "csv", delimiter=";")) == [{"a": "1", "b": "2"}]
    ragged = tmp_path / "r.csv"
    ragged.write_text("a,b\n1,2,3\n")
    with pytest.raises(CliError, match="line 2"):
        list(sources.read_rows(str(ragged), "csv"))


def test_json_lines_and_arrays(tmp_path: Path) -> None:
    lines = tmp_path / "a.jsonl"
    lines.write_text('{"a": 1}\n\n{"a": 2, "b": true}\n')
    assert list(sources.read_rows(str(lines), "jsonl")) == [{"a": 1}, {"a": 2, "b": True}]
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"a": 1}\n[1]\n')
    with pytest.raises(CliError, match="line 2 is not a JSON object"):
        list(sources.read_rows(str(bad), "jsonl"))
    array = tmp_path / "a.json"
    array.write_text(json.dumps([{"a": 1}, {"a": 2}]))
    assert len(list(sources.read_rows(str(array), "json"))) == 2
    array.write_text('{"a": 1}')
    with pytest.raises(CliError, match="array of objects"):
        list(sources.read_rows(str(array), "json"))


def test_parquet_keeps_types(tmp_path: Path) -> None:
    pa = pytest.importorskip("pyarrow")
    parquet = pytest.importorskip("pyarrow.parquet")
    table = pa.table(
        {
            "part_number": ["P-1", "P-2"],
            "unit_cost": [Decimal("1.50"), None],
            "hazardous": [True, False],
            "stock": pa.array([3, None], type=pa.int64()),
        }
    )
    path = tmp_path / "parts.parquet"
    parquet.write_table(table, path)
    rows = list(sources.read_rows(str(path), "parquet"))
    prepared = importing.prepare(SCHEMA, "records", "Part")
    coerced = list(importing.coerced_rows(rows, prepared, ignore_unknown=False))
    assert coerced == [
        {"part_number": "P-1", "unit_cost": "1.50", "hazardous": True, "stock": "3"},
        {"part_number": "P-2", "hazardous": False},
    ]


def test_missing_file_is_a_clean_error(tmp_path: Path) -> None:
    with pytest.raises(CliError, match=r"cannot read .*nope.csv"):
        list(sources.read_rows(str(tmp_path / "nope.csv"), "csv"))
    pytest.importorskip("pyarrow")
    with pytest.raises(CliError, match="cannot read"):
        list(sources.read_rows(str(tmp_path / "nope.parquet"), "parquet"))
