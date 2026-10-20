"""Small builders of table profiles for the inference tests."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path

from lorebase_core.infer import ColumnProfile, TableProfile

SUPPLY_DIR = Path(__file__).resolve().parents[4] / "examples" / "supply"


def table(name: str, /, **columns: Sequence[object]) -> TableProfile:
    """A profile with one column per keyword argument (values in row order)."""
    return TableProfile(
        name, tuple(ColumnProfile(column, list(values)) for column, values in columns.items())
    )


def rows(name: str, header: Sequence[str], body: Sequence[Sequence[object]]) -> TableProfile:
    """A profile from a header and rows."""
    return TableProfile(
        name,
        tuple(
            ColumnProfile(column, [row[index] for row in body])
            for index, column in enumerate(header)
        ),
    )


def csv_table(path: Path) -> TableProfile:
    """A profile of a CSV file (all values as text), named after the file stem."""
    with path.open(newline="", encoding="utf-8") as handle:
        header, *body = list(csv.reader(handle))
    return rows(path.stem, header, body)


def supply_tables() -> list[TableProfile]:
    return [csv_table(path) for path in sorted((SUPPLY_DIR / "data").glob("*.csv"))]
