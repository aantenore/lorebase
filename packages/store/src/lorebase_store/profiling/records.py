"""Profiler of rows read as records: JSON documents, JSON Lines, Parquet files, JSON APIs.

Rows are mappings from column name to native value (``str``, ``int``, ``float``, ``Decimal``,
``date``, ``datetime``, ``bool``, nested ``dict`` or ``list``, ``None``). Columns are the keys
seen in any row, in first-seen order; a row without a column holds null there. Every row is
counted and ``sample_rows`` of them are kept, the first ones (``head``) or a uniform seeded
sample (``reservoir``, kept in read order). Rows can be pushed one at a time
(``RecordProfiler``), so an asynchronous reader never holds more than the sample.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping, Sequence
from typing import Literal

from lorebase_core.infer import ColumnProfile, TableProfile
from lorebase_store.settings import RecordProfilingSettings


class RecordProfileError(ValueError):
    """The rows cannot be profiled as one table."""


class RowSampler[T]:
    """Counts rows and keeps ``size`` of them: the first ones, or a seeded uniform sample."""

    def __init__(self, size: int, sampling: Literal["head", "reservoir"], seed: int) -> None:
        self._size = size
        self._head = sampling == "head"
        self._random = random.Random(seed)
        self._kept: list[tuple[int, T]] = []
        self.total = 0

    def add(self, row: T) -> None:
        index = self.total
        self.total += 1
        if len(self._kept) < self._size:
            self._kept.append((index, row))
        elif not self._head:
            slot = self._random.randint(0, index)
            if slot < self._size:
                self._kept[slot] = (index, row)

    def kept(self) -> list[T]:
        """The kept rows, in read order."""
        return [row for _, row in sorted(self._kept, key=lambda item: item[0])]


class RecordProfiler:
    """Builds the ``TableProfile`` of rows pushed with ``add``."""

    def __init__(self, settings: RecordProfilingSettings) -> None:
        self._settings = settings
        self._sampler: RowSampler[Mapping[str, object]] = RowSampler(
            settings.sample_rows, settings.sampling, settings.seed
        )
        self._columns: dict[str, None] = {}

    def add(self, row: Mapping[str, object]) -> None:
        for column in row:
            if column not in self._columns:
                if len(self._columns) >= self._settings.max_columns:
                    raise RecordProfileError(
                        f"more than the {self._settings.max_columns} columns allowed"
                    )
                self._columns[column] = None
        self._sampler.add(row)

    def profile(self, name: str, columns: Sequence[str] = ()) -> TableProfile:
        """The profile of table ``name``; ``columns`` (a declared header) come first."""
        names = list(dict.fromkeys([*columns, *self._columns]))
        if len(names) > self._settings.max_columns:
            raise RecordProfileError(
                f"{name}: more than the {self._settings.max_columns} columns allowed"
            )
        kept = self._sampler.kept()
        return TableProfile(
            name,
            tuple(ColumnProfile(column, [row.get(column) for row in kept]) for column in names),
            row_count=self._sampler.total,
        )


def profile_records(
    rows: Iterable[Mapping[str, object]], name: str, settings: RecordProfilingSettings
) -> TableProfile:
    """Profile rows as table ``name``."""
    profiler = RecordProfiler(settings)
    for row in rows:
        profiler.add(row)
    return profiler.profile(name)
