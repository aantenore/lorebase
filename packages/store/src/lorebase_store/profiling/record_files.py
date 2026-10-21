"""Profilers of uploaded record files: JSON documents, JSON Lines and Parquet files.

The bytes are already bounded by the caller (an upload). A JSON document is an array of objects;
JSON Lines hold one object per line (blank lines skipped); both are UTF-8, with or without a
byte order mark. Parquet needs pyarrow (an optional extra of the installing package;
``ParquetUnavailableError`` without it) and is decoded ``batch_rows`` rows at a time. Values
keep their JSON or Arrow types and nested objects stay JSON values; the table is named after
the file (``delimited.table_name``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any

from lorebase_core.infer import TableProfile
from lorebase_store.profiling.delimited import table_name
from lorebase_store.profiling.records import RecordProfileError, RecordProfiler
from lorebase_store.settings import RecordProfilingSettings


class ParquetUnavailableError(RecordProfileError):
    """Parquet files cannot be read: pyarrow is not installed."""


def _text(data: bytes, name: str) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise RecordProfileError(f"{name}: not UTF-8 text") from None


def _add(profiler: RecordProfiler, row: Mapping[str, Any], name: str) -> None:
    try:
        profiler.add(row)
    except RecordProfileError as error:
        raise RecordProfileError(f"{name}: {error}") from None


def _profile(
    rows: Iterator[Mapping[str, Any]], name: str, settings: RecordProfilingSettings
) -> TableProfile:
    profiler = RecordProfiler(settings)
    for row in rows:
        _add(profiler, row, name)
    return profiler.profile(table_name(name))


def profile_json_bytes(data: bytes, name: str, settings: RecordProfilingSettings) -> TableProfile:
    """Profile a JSON document holding an array of objects."""
    try:
        document = json.loads(_text(data, name))
    except ValueError as error:
        raise RecordProfileError(f"{name}: not valid JSON ({error})") from None
    if not isinstance(document, list):
        raise RecordProfileError(f"{name}: the document is not an array of records")

    def rows() -> Iterator[Mapping[str, Any]]:
        for number, record in enumerate(document, start=1):
            if not isinstance(record, dict):
                raise RecordProfileError(f"{name}: record {number} is not an object")
            yield record

    return _profile(rows(), name, settings)


def profile_jsonl_bytes(data: bytes, name: str, settings: RecordProfilingSettings) -> TableProfile:
    """Profile JSON Lines: one object per line."""
    text = _text(data, name)

    def rows() -> Iterator[Mapping[str, Any]]:
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                raise RecordProfileError(f"{name}: line {number} is not valid JSON") from None
            if not isinstance(record, dict):
                raise RecordProfileError(f"{name}: line {number} is not an object")
            yield record

    return _profile(rows(), name, settings)


def profile_parquet_bytes(
    data: bytes, name: str, settings: RecordProfilingSettings
) -> TableProfile:
    """Profile a Parquet file; ``ParquetUnavailableError`` without pyarrow."""
    try:
        import pyarrow
        import pyarrow.parquet as parquet
    except ImportError:
        raise ParquetUnavailableError(f"{name}: Parquet files need pyarrow") from None
    profiler = RecordProfiler(settings)
    try:
        file = parquet.ParquetFile(pyarrow.BufferReader(data))
        columns = tuple(str(field.name) for field in file.schema_arrow)
        for batch in file.iter_batches(batch_size=settings.batch_rows):
            for row in batch.to_pylist():
                _add(profiler, row, name)
    except RecordProfileError:
        raise
    except (ValueError, OSError) as error:  # pyarrow.ArrowInvalid, truncated files
        raise RecordProfileError(f"{name}: not a readable Parquet file ({error})") from None
    return profiler.profile(table_name(name), columns)
