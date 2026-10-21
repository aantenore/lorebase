"""JSON, JSON Lines and Parquet connectors on local files."""

from __future__ import annotations

import builtins
import importlib.util
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from lorebase_core.model import Schema, Source
from lorebase_core.model.source import SourceType, SyncMode
from lorebase_scribe.settings import JsonSourceSettings, ParquetSourceSettings, SourceSettings
from lorebase_scribe.sources import records
from lorebase_scribe.sources.connectors import default_connector
from lorebase_scribe.sources.mapping import SourceDefinitionError
from lorebase_scribe.sources.records import (
    JsonConnector,
    JsonLinesConnector,
    ParquetConnector,
    after,
    flatten,
    records_at,
)

HAS_PYARROW = importlib.util.find_spec("pyarrow") is not None
needs_pyarrow = pytest.mark.skipif(not HAS_PYARROW, reason="pyarrow (the parquet extra) is absent")

RECORDS = [
    {
        "supplier_code": "S-1",
        "legal_name": "Acme",
        "updated": "2026-01-01",
        "address": {"city": "Turin", "geo": {"lat": 45.07}},
    },
    {"supplier_code": "S-2", "legal_name": "Beta", "updated": "2026-02-01", "tags": ["a", "b"]},
]


def json_source(
    schema: Schema, monkeypatch: pytest.MonkeyPatch, kind: SourceType, path: Path, **changes: Any
) -> Source:
    monkeypatch.setenv("TEST_RECORDS_LOCATION", str(path))
    found = schema.source_by_api_name("approved.suppliers")
    assert found is not None
    return found.model_copy(
        update={"type": kind, "connection": "secret://env/TEST_RECORDS_LOCATION", **changes}
    )


async def collect(connector: Any, since: Any = None) -> list[tuple[str, dict[str, Any]]]:
    return [(locator, dict(row)) async for locator, row in connector.rows(since)]


# Helpers ----------------------------------------------------------------------------------------


def test_flatten_nested_objects_with_a_separator() -> None:
    record = {"a": 1, "b": {"c": 2, "d": {"e": 3}}, "f": [1, {"g": 2}], "h": {}}
    assert flatten(record, None) == record
    assert flatten(record, ".") == {"a": 1, "b.c": 2, "b.d.e": 3, "f": [1, {"g": 2}], "h": {}}
    assert flatten(record, "__")["b__d__e"] == 3


def test_records_at_a_path() -> None:
    document = {"data": {"items": [{"a": 1}], "empty": None}}
    assert records_at(document, "data.items", "doc") == [{"a": 1}]
    assert records_at(document, "data.empty", "doc") == []
    assert records_at([{"a": 1}], None, "doc") == [{"a": 1}]
    with pytest.raises(SourceDefinitionError, match=r"no 'data\.missing'"):
        records_at(document, "data.missing", "doc")
    with pytest.raises(SourceDefinitionError, match="records_path"):
        records_at(document, None, "doc")
    with pytest.raises(SourceDefinitionError, match="record 2 is not an object"):
        records_at([{"a": 1}, 2], None, "doc")


def test_watermarks_compare_natively_or_as_text() -> None:
    assert after(5, 4)
    assert not after(4, 4)
    assert after("2026-02-01", "2026-01-15")
    assert after(datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    assert after("10", 9) is False  # "10" < "9" as text: mixed types compare as text
    assert not after(None, 1)
    assert not after("", 1)


# JSON -------------------------------------------------------------------------------------------


async def test_json_array_document(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "suppliers.json"
    path.write_text(json.dumps(RECORDS))
    source = json_source(supply_schema, monkeypatch, SourceType.JSON, path)
    connector = JsonConnector(source, JsonSourceSettings(allowed_roots=(str(tmp_path),)))
    rows = await collect(connector)
    assert connector.columns == ()
    assert [locator for locator, _ in rows] == ["suppliers.json:1", "suppliers.json:2"]
    assert rows[0][1]["address"] == {"city": "Turin", "geo": {"lat": 45.07}}  # a JSON value
    assert rows[1][1]["tags"] == ["a", "b"]


async def test_json_records_path_flattening_and_watermark(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "export.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"data": {"suppliers": RECORDS}}).encode())
    source = json_source(
        supply_schema,
        monkeypatch,
        SourceType.JSON,
        path,
        records_path="data.suppliers",
        flatten_separator=".",
        sync_mode=SyncMode.INCREMENTAL,
        watermark_column="updated",
    )
    connector = JsonConnector(source, JsonSourceSettings(allowed_roots=(str(tmp_path),)))
    rows = await collect(connector)
    assert rows[0][1]["address.geo.lat"] == 45.07
    assert rows[0][1]["address.city"] == "Turin"
    assert [row["supplier_code"] for _, row in await collect(connector, "2026-01-15")] == ["S-2"]


async def test_json_refusals(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "suppliers.json"
    path.write_text(json.dumps(RECORDS))
    source = json_source(supply_schema, monkeypatch, SourceType.JSON, path)
    with pytest.raises(SourceDefinitionError, match=r"scribe\.sources\.json_files\.allowed_roots"):
        await collect(JsonConnector(source, JsonSourceSettings()))
    small = JsonSourceSettings(allowed_roots=(str(tmp_path),), max_bytes=10)
    with pytest.raises(SourceDefinitionError, match=r"json_files\.max_bytes"):
        await collect(JsonConnector(source, small))
    path.write_text("{not json")
    allowed = JsonSourceSettings(allowed_roots=(str(tmp_path),))
    with pytest.raises(SourceDefinitionError, match="not valid JSON"):
        await collect(JsonConnector(source, allowed))
    (tmp_path / "link.json").symlink_to(tmp_path.parent)
    linked = json_source(supply_schema, monkeypatch, SourceType.JSON, tmp_path / "link.json")
    with pytest.raises(SourceDefinitionError, match="allowed_roots"):
        await collect(JsonConnector(linked, allowed))


# JSON Lines -------------------------------------------------------------------------------------


async def test_json_lines_are_streamed(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "suppliers.jsonl"
    lines = [json.dumps(record) for record in RECORDS]
    path.write_bytes(b"\xef\xbb\xbf" + f"{lines[0]}\r\n\n{lines[1]}".encode())  # no final newline
    monkeypatch.setattr(records, "READ_CHUNK", 7)  # lines span several reads
    source = json_source(supply_schema, monkeypatch, SourceType.JSONL, path, flatten_separator="_")
    connector = JsonLinesConnector(source, JsonSourceSettings(allowed_roots=(str(tmp_path),)))
    rows = await collect(connector)
    assert [locator for locator, _ in rows] == ["suppliers.jsonl:1", "suppliers.jsonl:3"]
    assert rows[0][1]["address_city"] == "Turin"
    assert "address" not in rows[0][1]


async def test_json_lines_refusals(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "suppliers.jsonl"
    source = json_source(supply_schema, monkeypatch, SourceType.JSONL, path)
    settings = JsonSourceSettings(allowed_roots=(str(tmp_path),), max_line_bytes=64)
    path.write_text('{"a": 1}\n[1, 2]\n')
    with pytest.raises(SourceDefinitionError, match="line 2 is not an object"):
        await collect(JsonLinesConnector(source, settings))
    path.write_text('{"a": 1}\n{"a": \n')
    with pytest.raises(SourceDefinitionError, match="line 2 is not valid JSON"):
        await collect(JsonLinesConnector(source, settings))
    path.write_text('{"a": 1}\n{"a": "' + "x" * 100 + '"}\n')
    with pytest.raises(SourceDefinitionError, match=r"line 2 is longer .*max_line_bytes"):
        await collect(JsonLinesConnector(source, settings))
    path.write_text('{"a": "' + "x" * 100)  # an unterminated line past the limit
    with pytest.raises(SourceDefinitionError, match="line 1 is longer"):
        await collect(JsonLinesConnector(source, settings))


async def test_the_factory_builds_record_connectors(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "suppliers.jsonl"
    settings = SourceSettings(allowed_connections=("secret://env/TEST_RECORDS_LOCATION",))
    for kind, expected in (
        (SourceType.JSON, JsonConnector),
        (SourceType.JSONL, JsonLinesConnector),
        (SourceType.PARQUET, ParquetConnector),
    ):
        source = json_source(supply_schema, monkeypatch, kind, path)
        assert isinstance(default_connector(source, settings), expected)


# Parquet ----------------------------------------------------------------------------------------


def write_parquet(path: Path, row_group_size: int = 2) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table(
        {
            "supplier_code": ["S-1", "S-2", "S-3"],
            "risk_score": [10, None, 30],
            "amount": pa.array([Decimal("1.50"), Decimal("2.25"), None], pa.decimal128(9, 2)),
            "updated": pa.array(
                [datetime(2026, 1, d, tzinfo=UTC) for d in (1, 2, 3)], pa.timestamp("us", tz="UTC")
            ),
            "address": [{"city": "Turin", "zip": "10100"}, {"city": "Milan", "zip": None}, None],
        }
    )
    pq.write_table(table, path, row_group_size=row_group_size)


@needs_pyarrow
async def test_parquet_files_are_read_in_batches(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "suppliers.parquet"
    write_parquet(path)
    source = json_source(supply_schema, monkeypatch, SourceType.PARQUET, path)
    settings = ParquetSourceSettings(allowed_roots=(str(tmp_path),), batch_rows=1)
    connector = ParquetConnector(source, settings)
    rows = await collect(connector)
    assert connector.columns == ("supplier_code", "risk_score", "amount", "updated", "address")
    assert [locator for locator, _ in rows] == [f"suppliers.parquet:{n}" for n in (1, 2, 3)]
    assert rows[0][1]["amount"] == Decimal("1.50")
    assert rows[0][1]["address"] == {"city": "Turin", "zip": "10100"}
    assert rows[1][1]["risk_score"] is None


@needs_pyarrow
async def test_parquet_struct_flattening_and_watermark(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "suppliers.parquet"
    write_parquet(path)
    source = json_source(
        supply_schema,
        monkeypatch,
        SourceType.PARQUET,
        path,
        flatten_separator=".",
        sync_mode=SyncMode.INCREMENTAL,
        watermark_column="updated",
    )
    connector = ParquetConnector(source, ParquetSourceSettings(allowed_roots=(str(tmp_path),)))
    rows = await collect(connector, datetime(2026, 1, 1, 12, tzinfo=UTC))
    assert connector.columns == (
        "supplier_code",
        "risk_score",
        "amount",
        "updated",
        "address.city",
        "address.zip",
    )
    assert [row["supplier_code"] for _, row in rows] == ["S-2", "S-3"]
    assert rows[0][1]["address.city"] == "Milan"


@needs_pyarrow
async def test_parquet_refusals(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "suppliers.parquet"
    path.write_bytes(b"not a parquet file")
    source = json_source(supply_schema, monkeypatch, SourceType.PARQUET, path)
    with pytest.raises(SourceDefinitionError, match=r"scribe\.sources\.parquet\.allowed_roots"):
        await collect(ParquetConnector(source, ParquetSourceSettings()))
    allowed = ParquetSourceSettings(allowed_roots=(str(tmp_path),))
    with pytest.raises(SourceDefinitionError, match="not a Parquet file"):
        await collect(ParquetConnector(source, allowed))


async def test_parquet_without_pyarrow_names_the_extra(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_import = builtins.__import__

    def no_pyarrow(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pyarrow" or name.startswith("pyarrow."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pyarrow)
    source = json_source(supply_schema, monkeypatch, SourceType.PARQUET, tmp_path / "x.parquet")
    settings = ParquetSourceSettings(allowed_roots=(str(tmp_path),))
    with pytest.raises(SourceDefinitionError, match=r"lorebase-scribe\[parquet\]"):
        await collect(ParquetConnector(source, settings))
