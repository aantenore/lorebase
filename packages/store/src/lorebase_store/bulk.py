"""Bulk writes: multi-row INSERT for ordinary batches, COPY through a staging table for large ones.

Both paths end in one set-based statement per batch with the same conflict handling, so the
caller does not need to know which one ran.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from enum import StrEnum
from typing import Any

from sqlalchemy import Table, column, select, table, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.settings import WriteSettings

STAGE_PREFIX = "lb_stage_"
"""Prefix of the temporary staging tables used by the COPY path (dropped at session end)."""


class OnConflict(StrEnum):
    """What a bulk write does with rows whose primary key already exists."""

    IGNORE = "ignore"
    UPDATE = "update"
    ERROR = "error"


def chunks[T](items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    """Consecutive slices of at most `size` items."""
    if size < 1:
        raise ValueError("chunk size must be positive")
    for start in range(0, len(items), size):
        yield items[start : start + size]


def uses_copy(rows: int, writes: WriteSettings) -> bool:
    """Whether a batch of `rows` rows takes the COPY path."""
    return writes.copy_threshold_rows > 0 and rows >= writes.copy_threshold_rows


async def bulk_write(
    session: AsyncSession,
    target: Table,
    rows: Sequence[Mapping[str, Any]],
    *,
    writes: WriteSettings,
    on_conflict: OnConflict = OnConflict.IGNORE,
    update_columns: Sequence[str] = (),
) -> None:
    """Insert `rows` (all with the same keys) into `target`.

    `OnConflict.UPDATE` overwrites `update_columns` of existing rows; `IGNORE` keeps them.
    """
    if not rows:
        return
    columns = list(rows[0])
    if on_conflict is OnConflict.UPDATE and not update_columns:
        raise ValueError("an updating bulk write needs update_columns")
    if uses_copy(len(rows), writes):
        await _copy_write(session, target, columns, rows, on_conflict, update_columns)
        return
    for chunk in chunks(rows, writes.insert_batch_rows):
        statement = _with_conflict(pg_insert(target), target, on_conflict, update_columns)
        await session.execute(statement, list(chunk))


def _with_conflict(
    statement: Any, target: Table, on_conflict: OnConflict, update_columns: Sequence[str]
) -> Any:
    if on_conflict is OnConflict.IGNORE:
        return statement.on_conflict_do_nothing()
    if on_conflict is OnConflict.UPDATE:
        keys = [c.name for c in target.primary_key.columns]
        return statement.on_conflict_do_update(
            index_elements=keys,
            set_={name: statement.excluded[name] for name in update_columns},
        )
    return statement


async def _copy_write(
    session: AsyncSession,
    target: Table,
    columns: list[str],
    rows: Sequence[Mapping[str, Any]],
    on_conflict: OnConflict,
    update_columns: Sequence[str],
) -> None:
    stage = STAGE_PREFIX + target.name
    await session.execute(
        text(
            f"CREATE TEMP TABLE IF NOT EXISTS {stage} "
            f"(LIKE {target.name} INCLUDING DEFAULTS) ON COMMIT DELETE ROWS"
        )
    )
    json_columns = {c.name for c in target.columns if isinstance(c.type, JSONB)}
    records = [
        tuple(
            json.dumps(row[name]) if name in json_columns and row[name] is not None else row[name]
            for name in columns
        )
        for row in rows
    ]
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    driver: Any = raw.driver_connection
    await driver.copy_records_to_table(stage, records=records, columns=columns)
    staged = table(stage, *(column(name) for name in columns))
    statement = pg_insert(target).from_select(columns, select(*staged.c))
    await session.execute(_with_conflict(statement, target, on_conflict, update_columns))
    await session.execute(text(f"TRUNCATE {stage}"))
