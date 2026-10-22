"""Readers of import files: CSV, TSV, JSON Lines, a JSON array and Parquet.

Each reader yields one dict per row, lazily, so that a large file is never held in memory
whole (a JSON array is the exception). The format comes from `--format` or the file suffix.
Parquet needs `pyarrow`, an optional dependency (`pip install "lorebase-cli[parquet]"`).
"""

from __future__ import annotations

import csv
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from lorebase_cli.failures import CliError

STDIN = "-"
FORMATS = ("csv", "tsv", "jsonl", "json", "parquet")
SUFFIXES = {
    ".csv": "csv",
    ".tsv": "tsv",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".json": "json",
    ".parquet": "parquet",
}
PARQUET_BATCH_ROWS = 10_000


def detect_format(path: str, given: str | None) -> str:
    """The format named, or the one the suffix of `path` implies."""
    if given:
        if given not in FORMATS:
            raise CliError(f"unknown format {given!r}; use one of {', '.join(FORMATS)}")
        return given
    suffix = Path(path).suffix.lower()
    if suffix in SUFFIXES:
        return SUFFIXES[suffix]
    raise CliError(
        f"cannot tell the format of {path!r} from its suffix; pass --format ({', '.join(FORMATS)})"
    )


def read_rows(path: str, fmt: str, *, delimiter: str | None = None) -> Iterator[dict[str, Any]]:
    """Rows of the file (`-` is standard input, for text formats)."""
    if fmt == "parquet":
        if path == STDIN:
            raise CliError("Parquet cannot be read from standard input")
        return _parquet(Path(path))
    if fmt in ("csv", "tsv"):
        return _csv(path, delimiter or ("\t" if fmt == "tsv" else ","))
    if fmt == "jsonl":
        return _jsonl(path)
    return _json_array(path)


def _lines(path: str) -> Iterator[str]:
    if path == STDIN:
        yield from sys.stdin
        return
    try:
        handle = Path(path).open(encoding="utf-8-sig", newline="")  # noqa: SIM115 - closed below
    except OSError as error:
        raise CliError(f"cannot read {path}: {error.strerror or error}") from error
    with handle:
        yield from handle


def _csv(path: str, delimiter: str) -> Iterator[dict[str, Any]]:
    reader = csv.DictReader(_lines(path), delimiter=delimiter)
    if reader.fieldnames is None:
        raise CliError(f"{path}: no header row")
    for number, row in enumerate(reader, start=2):
        if None in row:
            raise CliError(f"{path}: line {number} has more cells than the header")
        yield row


def _jsonl(path: str) -> Iterator[dict[str, Any]]:
    for number, line in enumerate(_lines(path), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise CliError(f"{path}: line {number} is not valid JSON") from None
        if not isinstance(row, dict):
            raise CliError(f"{path}: line {number} is not a JSON object")
        yield row


def _json_array(path: str) -> Iterator[dict[str, Any]]:
    text = "".join(_lines(path))
    try:
        rows = json.loads(text)
    except ValueError:
        raise CliError(f"{path}: not valid JSON") from None
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise CliError(f"{path}: expected a JSON array of objects")
    yield from rows


def _parquet(path: Path) -> Iterator[dict[str, Any]]:
    try:
        import pyarrow.parquet as parquet  # type: ignore[import-untyped]
    except ImportError:
        raise CliError(
            "reading Parquet needs pyarrow: pip install 'lorebase-cli[parquet]'"
        ) from None
    try:
        file = parquet.ParquetFile(path)
    except (OSError, ValueError) as error:  # missing, unreadable or not a Parquet file
        raise CliError(f"cannot read {path}: {error}") from error
    with file:
        for batch in file.iter_batches(batch_size=PARQUET_BATCH_ROWS):
            yield from batch.to_pylist()
