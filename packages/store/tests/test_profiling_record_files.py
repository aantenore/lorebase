"""Profiles of uploaded JSON, JSON Lines and Parquet files."""

from __future__ import annotations

import io
import json
import sys
from datetime import date
from typing import Any

import pytest

from lorebase_store.profiling import (
    ParquetUnavailableError,
    RecordProfileError,
    profile_json_bytes,
    profile_jsonl_bytes,
    profile_parquet_bytes,
)
from lorebase_store.settings import RecordProfilingSettings

ROWS: list[dict[str, Any]] = [
    {"id": 1, "name": "Kavsen", "address": {"city": "Gdansk"}},
    {"id": 2, "name": "Sundello", "tags": ["a"]},
]


def _parquet(rows: list[dict[str, Any]]) -> bytes:
    import pyarrow
    import pyarrow.parquet as parquet

    sink = io.BytesIO()
    parquet.write_table(pyarrow.Table.from_pylist(rows), sink)
    return sink.getvalue()


def test_json_document() -> None:
    data = b"\xef\xbb\xbf" + json.dumps(ROWS).encode()
    profile = profile_json_bytes(data, "dir/suppliers.json", RecordProfilingSettings())
    assert profile.name == "suppliers"
    assert profile.row_count == 2
    assert [c.name for c in profile.columns] == ["id", "name", "address", "tags"]
    assert list(profile.columns[2].values) == [{"city": "Gdansk"}, None]


def test_json_lines_skip_blank_lines() -> None:
    data = b"\n".join(json.dumps(row).encode() for row in ROWS) + b"\n\n"
    profile = profile_jsonl_bytes(data, "suppliers.jsonl", RecordProfilingSettings())
    assert profile.row_count == 2
    assert list(profile.columns[0].values) == [1, 2]


def test_parquet_keeps_arrow_types_and_batches() -> None:
    rows = [{"id": n, "day": date(2026, 1, 1 + n)} for n in range(5)]
    settings = RecordProfilingSettings(batch_rows=2, sampling="head")
    profile = profile_parquet_bytes(_parquet(rows), "days.parquet", settings)
    assert profile.name == "days"
    assert profile.row_count == 5
    assert [c.name for c in profile.columns] == ["id", "day"]
    assert list(profile.columns[1].values) == [date(2026, 1, 1 + n) for n in range(5)]


def test_parquet_without_pyarrow(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _parquet(ROWS[:1])
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    with pytest.raises(ParquetUnavailableError, match="need pyarrow"):
        profile_parquet_bytes(data, "a.parquet", RecordProfilingSettings())


def test_too_many_columns_name_the_file() -> None:
    data = json.dumps([{"a": 1, "b": 2, "c": 3}]).encode()
    with pytest.raises(RecordProfileError, match=r"^wide\.json: more than the 2 columns"):
        profile_json_bytes(data, "wide.json", RecordProfilingSettings(max_columns=2))


def test_a_wide_parquet_file_is_refused() -> None:
    data = _parquet([{"a": 1, "b": 2, "c": 3}])
    with pytest.raises(RecordProfileError, match="more than the 2 columns"):
        profile_parquet_bytes(data, "wide.parquet", RecordProfilingSettings(max_columns=2))
