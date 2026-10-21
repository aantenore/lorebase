"""CSV and TSV profiler: delimiter sniffing, encoding fallback and row sampling (stdlib ``csv``).

The first row is the header. Every row is counted; ``sample_rows`` of them are kept, either the
first ones (``head``) or a uniform seeded sample (``reservoir``, rows kept in file order). Short
rows are padded with nulls and extra cells are dropped. All values stay text: inference
detects their types.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator
from itertools import chain
from pathlib import Path
from typing import TextIO

from lorebase_core.infer import ColumnProfile, TableProfile
from lorebase_store.profiling.records import RowSampler
from lorebase_store.settings import CsvProfilingSettings

TSV_SUFFIXES = (".tsv", ".tab")
DEFAULT_DELIMITER = ","


class CsvProfileError(ValueError):
    """The file cannot be read as delimited text."""


def _delimiter(sample: str, name: str, settings: CsvProfilingSettings) -> str:
    if name.lower().endswith(TSV_SUFFIXES):
        return "\t"
    try:
        return csv.Sniffer().sniff(sample, delimiters=settings.delimiters).delimiter
    except csv.Error:
        return DEFAULT_DELIMITER


def _sample(
    rows: Iterator[list[str]], settings: CsvProfilingSettings
) -> tuple[list[list[str]], int]:
    """Kept rows (in file order) and the number of rows read."""
    sampler: RowSampler[list[str]] = RowSampler(
        settings.sample_rows, settings.sampling, settings.seed
    )
    for row in rows:
        sampler.add(row)
    return sampler.kept(), sampler.total


def _header(cells: list[str]) -> list[str]:
    return [cell.strip() or f"column_{index + 1}" for index, cell in enumerate(cells)]


def profile_text(stream: TextIO, name: str, settings: CsvProfilingSettings) -> TableProfile:
    """Profile delimited text; ``name`` is the table name (a file name keeps its suffix rules)."""
    head = stream.read(settings.sniff_bytes)
    head += stream.readline()
    delimiter = _delimiter(head, name, settings)
    lines: Iterable[str] = chain(io.StringIO(head), stream)
    reader = csv.reader(lines, delimiter=delimiter)
    try:
        first = next(reader, None)
        if first is None:
            raise CsvProfileError(f"{name}: the file is empty")
        header = _header(first)
        if len(header) > settings.max_columns:
            raise CsvProfileError(
                f"{name}: {len(header)} columns, more than the {settings.max_columns} allowed"
            )
        rows = (row for row in reader if any(cell.strip() for cell in row))
        kept, total = _sample(rows, settings)
    except csv.Error as error:
        raise CsvProfileError(f"{name}: not valid delimited text ({error})") from None
    columns = tuple(
        ColumnProfile(
            column,
            [row[index] if index < len(row) else None for row in kept],
        )
        for index, column in enumerate(header)
    )
    return TableProfile(table_name(name), columns, row_count=total)


def table_name(file_name: str) -> str:
    """Table name of a file: the base name without directories and suffix."""
    return Path(file_name.replace("\\", "/")).stem or file_name


def profile_bytes(data: bytes, name: str, settings: CsvProfilingSettings) -> TableProfile:
    """Profile an uploaded file, trying ``settings.encodings`` in order."""
    for encoding in settings.encodings:
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        return profile_text(io.StringIO(text, newline=""), name, settings)
    raise CsvProfileError(f"{name}: not decodable as {', '.join(settings.encodings)}")


def profile_file(path: Path, settings: CsvProfilingSettings) -> TableProfile:
    """Profile a file on disk without loading it whole, trying ``settings.encodings``."""
    for encoding in settings.encodings:
        try:
            with path.open(encoding=encoding, newline="") as stream:
                return profile_text(stream, path.name, settings)
        except (UnicodeDecodeError, LookupError):
            continue
    raise CsvProfileError(f"{path.name}: not decodable as {', '.join(settings.encodings)}")
