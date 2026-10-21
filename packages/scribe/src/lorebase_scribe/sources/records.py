"""Readers of Sources whose rows are records: JSON documents, JSON Lines and Parquet files.

The connection's value is a local path (or a `file:` URL) under the `allowed_roots` of the
connector's settings section, opened through the guards of `access` (no symbolic link
followed after the roots check, regular files only).

- `json`: the document is parsed whole (at most `scribe.sources.json_files.max_bytes`); its
  records are the array at `records_path` (dotted keys, `data.items`), or the document itself
  when it is an array. Locators are `file.json:<record number>`.
- `jsonl`: one object per line, streamed; lines are at most `max_line_bytes`, blank lines are
  skipped. Locators are `file.jsonl:<line number>`.
- `parquet`: needs pyarrow (`lorebase-scribe[parquet]`); the file is streamed in batches of
  `scribe.sources.parquet.batch_rows` rows, never read whole. Locators are
  `file.parquet:<row number>`.

Values keep their JSON (or Arrow) types: nested objects and arrays are JSON values, which a
`json` Attribute stores as they are and textual Attributes receive as canonical JSON text. With
`flatten_separator`, nested objects become columns named `parent<separator>child` instead
(arrays stay values). Records have no fixed columns: a key missing from a record is null
there (Parquet columns come from the file schema). Incremental reads compare the watermark
column with the previous watermark natively, or as text when the types differ.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from lorebase_core.model import Source
from lorebase_scribe.settings import JsonSourceSettings, ParquetSourceSettings
from lorebase_scribe.sources.access import (
    READ_CHUNK,
    SourceReadError,
    local_path,
    open_local,
    read_bounded,
    resolve_location,
)
from lorebase_scribe.sources.mapping import Row, SourceDefinitionError, text_of

JSON_SECTION = "scribe.sources.json_files"
PARQUET_SECTION = "scribe.sources.parquet"
PARQUET_EXTRA = "lorebase-scribe[parquet]"


# Record helpers -------------------------------------------------------------------------------


def value_at(document: Any, path: str) -> Any:
    """The value at a dotted path of nested objects; `KeyError` when a step is missing."""
    node = document
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(path)
        node = node[part]
    return node


def records_at(document: Any, path: str | None, where: str) -> list[Mapping[str, Any]]:
    """The records of a parsed document: the array at `path`, or the document itself."""
    node = document
    if path is not None:
        try:
            node = value_at(document, path)
        except KeyError:
            raise SourceDefinitionError(f"{where}: no {path!r} in the document") from None
        if node is None:
            return []
    if not isinstance(node, list):
        what = "the document" if path is None else repr(path)
        hint = "" if path is not None else " (set records_path)"
        raise SourceDefinitionError(f"{where}: {what} is not an array of records{hint}")
    for index, record in enumerate(node, start=1):
        if not isinstance(record, dict):
            raise SourceDefinitionError(f"{where}: record {index} is not an object")
    return node


def flatten(record: Mapping[str, Any], separator: str | None) -> dict[str, Any]:
    """The record with nested objects as `parent<separator>child` columns; unchanged (a copy)
    without a separator. Empty objects and arrays stay values."""
    if separator is None:
        return dict(record)
    flat: dict[str, Any] = {}

    def walk(prefix: str, node: Mapping[str, Any]) -> None:
        for key, value in node.items():
            name = f"{prefix}{separator}{key}" if prefix else str(key)
            if isinstance(value, dict) and value:
                walk(name, value)
            else:
                flat[name] = value

    walk("", record)
    return flat


def after(value: Any, since: Any) -> bool:
    """Whether a watermark value is greater than `since` (natively, else as text)."""
    if value is None or value == "":
        return False
    try:
        return bool(value > since)
    except TypeError:
        return text_of(value) > text_of(since)


def keep(row: Row, source: Source, since: Any) -> bool:
    """Whether an incremental read keeps the row (always, without a previous watermark)."""
    column = source.watermark_column
    return since is None or column is None or after(row.get(column), since)


# JSON and JSON Lines --------------------------------------------------------------------------


@dataclass
class JsonConnector:
    """A JSON document holding an array of records."""

    source: Source
    settings: JsonSourceSettings

    @property
    def columns(self) -> tuple[str, ...]:
        return ()

    def _read(self, path: Path) -> bytes:
        descriptor = open_local(path, self.settings.allowed_roots, JSON_SECTION)
        try:
            return read_bounded(
                descriptor, path.name, self.settings.max_bytes, f"{JSON_SECTION}.max_bytes"
            )
        except OSError as error:
            raise SourceReadError(f"{path.name}: {error.strerror}") from None
        finally:
            os.close(descriptor)

    async def rows(self, since: Any) -> AsyncIterator[tuple[str, Row]]:
        path = local_path(resolve_location(self.source), "JSON")
        data = await asyncio.to_thread(self._read, path)
        try:
            document = json.loads(data)
        except ValueError as error:
            raise SourceDefinitionError(f"{path.name}: not valid JSON ({error})") from None
        separator = self.source.flatten_separator
        records = records_at(document, self.source.records_path, path.name)
        for number, record in enumerate(records, start=1):
            row = flatten(record, separator)
            if keep(row, self.source, since):
                yield f"{path.name}:{number}", row


@dataclass
class JsonLinesConnector:
    """A JSON Lines file: one object per line, streamed."""

    source: Source
    settings: JsonSourceSettings

    @property
    def columns(self) -> tuple[str, ...]:
        return ()

    async def _lines(self, descriptor: int, name: str) -> AsyncIterator[tuple[int, bytes]]:
        """Numbered lines, read `READ_CHUNK` bytes at a time; none longer than the limit."""
        limit = self.settings.max_line_bytes
        buffer = b""
        number = 0
        while True:
            try:
                chunk = await asyncio.to_thread(os.read, descriptor, READ_CHUNK)
            except OSError as error:
                raise SourceReadError(f"{name}: {error.strerror}") from None
            if not chunk:
                if buffer:  # a last line without a newline
                    yield number + 1, buffer
                return
            *complete, buffer = (buffer + chunk).split(b"\n")
            for line in complete:
                number += 1
                if len(line) > limit:
                    raise self._too_long(name, number)
                yield number, line
            if len(buffer) > limit:
                raise self._too_long(name, number + 1)

    def _too_long(self, name: str, number: int) -> SourceDefinitionError:
        return SourceDefinitionError(
            f"{name}: line {number} is longer than the {self.settings.max_line_bytes} bytes "
            f"allowed by {JSON_SECTION}.max_line_bytes"
        )

    async def rows(self, since: Any) -> AsyncIterator[tuple[str, Row]]:
        path = local_path(resolve_location(self.source), "JSON Lines")
        descriptor = await asyncio.to_thread(
            open_local, path, self.settings.allowed_roots, JSON_SECTION
        )
        separator = self.source.flatten_separator
        try:
            async for number, line in self._lines(descriptor, path.name):
                if number == 1:
                    line = line.removeprefix(b"\xef\xbb\xbf")
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    raise SourceDefinitionError(
                        f"{path.name}: line {number} is not valid JSON"
                    ) from None
                if not isinstance(record, dict):
                    raise SourceDefinitionError(f"{path.name}: line {number} is not an object")
                row = flatten(record, separator)
                if keep(row, self.source, since):
                    yield f"{path.name}:{number}", row
        finally:
            os.close(descriptor)


# Parquet --------------------------------------------------------------------------------------


def _pyarrow() -> tuple[ModuleType, ModuleType]:
    """`pyarrow` and `pyarrow.parquet`, or a definition error naming the extra."""
    try:
        import pyarrow
        import pyarrow.parquet as parquet
    except ImportError:
        raise SourceDefinitionError(
            f"parquet Sources need pyarrow: install {PARQUET_EXTRA}"
        ) from None
    return pyarrow, parquet


def _column_names(pyarrow: ModuleType, schema: Any, separator: str | None) -> tuple[str, ...]:
    """Column names of an Arrow schema; struct fields flattened as rows are."""

    def names(field: Any, prefix: str) -> Iterator[str]:
        name = f"{prefix}{separator}{field.name}" if prefix else str(field.name)
        if separator is not None and pyarrow.types.is_struct(field.type) and field.type.num_fields:
            for index in range(field.type.num_fields):
                yield from names(field.type.field(index), name)
        else:
            yield name

    return tuple(name for field in schema for name in names(field, ""))


@dataclass
class ParquetConnector:
    """A Parquet file, read batch by batch."""

    source: Source
    settings: ParquetSourceSettings
    _columns: tuple[str, ...] = ()

    @property
    def columns(self) -> tuple[str, ...]:
        return self._columns

    async def rows(self, since: Any) -> AsyncIterator[tuple[str, Row]]:
        pyarrow, parquet = _pyarrow()
        path = local_path(resolve_location(self.source), "Parquet")
        descriptor = await asyncio.to_thread(
            open_local, path, self.settings.allowed_roots, PARQUET_SECTION
        )
        separator = self.source.flatten_separator
        with os.fdopen(descriptor, "rb") as handle:
            try:
                file = parquet.ParquetFile(handle)
                self._columns = _column_names(pyarrow, file.schema_arrow, separator)
                batches = file.iter_batches(batch_size=self.settings.batch_rows)
            except ValueError as error:  # pyarrow.ArrowInvalid: not a Parquet file
                raise SourceDefinitionError(f"{path.name}: not a Parquet file ({error})") from None
            except OSError as error:
                raise SourceReadError(f"{path.name}: {error}") from None
            number = 0
            while True:
                try:
                    batch = await asyncio.to_thread(next, batches, None)
                except ValueError as error:
                    raise SourceDefinitionError(f"{path.name}: {error}") from None
                except OSError as error:
                    raise SourceReadError(f"{path.name}: {error}") from None
                if batch is None:
                    return
                for record in batch.to_pylist():
                    number += 1
                    row = flatten(record, separator)
                    if keep(row, self.source, since):
                        yield f"{path.name}:{number}", row
