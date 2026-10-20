"""Input of schema inference: what a profiler saw of each table.

A profiler (CSV reader, database catalog reader) turns a structured source into
``TableProfile`` objects; inference never reads files or connects to anything. Values are
Python natives (``str`` from CSV; ``int``, ``Decimal``, ``date``, ``bool``... from databases);
``None`` is null. A profile may also carry what a catalog declares (types, nullability, keys,
foreign keys, comments): declared facts win over facts seen in values.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from lorebase_core.types import TypeSpec


@dataclass(frozen=True, slots=True)
class DeclaredForeignKey:
    """A foreign key constraint of the source catalog."""

    columns: tuple[str, ...]
    target_table: str
    target_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ColumnProfile:
    """One column: its values (sampled or complete) and what a catalog declares about it.

    ``values`` may be any iterable, including a generator over a complete column; inference
    reads at most ``InferenceOptions.sample_size`` of them.
    """

    name: str
    values: Iterable[object] = ()
    declared_type: TypeSpec | None = None
    declared_max_length: int | None = None
    nullable: bool | None = None
    unique: bool | None = None
    comment: str | None = None


@dataclass(frozen=True, slots=True)
class TableProfile:
    """One table. ``row_count`` is the size of the whole table when the values are a sample;
    ``None`` means the values are the whole table."""

    name: str
    columns: tuple[ColumnProfile, ...]
    row_count: int | None = None
    primary_key: tuple[str, ...] | None = None
    foreign_keys: tuple[DeclaredForeignKey, ...] = field(default=())
    comment: str | None = None
