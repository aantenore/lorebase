"""Postgres profiler: catalog reflection (SQLAlchemy) plus a sampled, read-only SELECT per table.

Declared facts come from the catalog: column types (mapped to Attribute types with a fixed
table, design 9.1), nullability, primary keys, single-column unique constraints, foreign keys
and comments. Values come from ``SELECT ... LIMIT sample_rows`` in a read-only transaction
with a statement timeout. Row counts are the planner estimate unless ``exact_row_count``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, column, func, inspect, select, table, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.types import (
    ARRAY,
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    Integer,
    Interval,
    Numeric,
    String,
    Text,
    TypeEngine,
    Uuid,
)

from lorebase_core.infer import ColumnProfile, DeclaredForeignKey, TableProfile
from lorebase_core.types import AttributeType, TypeSpec
from lorebase_store.settings import PostgresProfilingSettings

DEFAULT_SCHEMA = "public"
_ESTIMATE = text(
    "SELECT c.reltuples::bigint FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = :schema AND c.relname = :table"
)


class PostgresProfileError(ValueError):
    """The requested tables cannot be profiled."""


@dataclass(frozen=True)
class _Column:
    name: str
    spec: TypeSpec | None
    max_length: int | None
    nullable: bool
    unique: bool
    comment: str | None


@dataclass(frozen=True)
class _Table:
    name: str
    columns: tuple[_Column, ...]
    primary_key: tuple[str, ...]
    foreign_keys: tuple[DeclaredForeignKey, ...]
    comment: str | None


def type_spec(sql_type: TypeEngine[Any]) -> tuple[TypeSpec | None, int | None]:
    """Attribute type and declared max length of a catalog type; ``(None, None)`` when the type
    has no fixed mapping (inference then detects it from values)."""
    if isinstance(sql_type, Boolean):
        return TypeSpec.of(AttributeType.BOOLEAN), None
    if isinstance(sql_type, Integer):
        return TypeSpec.of(AttributeType.INTEGER), None
    if isinstance(sql_type, Float):
        return TypeSpec.of(AttributeType.FLOAT), None
    if isinstance(sql_type, Numeric):
        precision, scale = sql_type.precision, sql_type.scale
        if precision is not None and scale is not None and 0 <= scale <= precision:
            return TypeSpec.decimal(precision, scale), None
        return None, None
    if isinstance(sql_type, DateTime):
        return TypeSpec.of(AttributeType.TIMESTAMP), None
    if isinstance(sql_type, Date):
        return TypeSpec.of(AttributeType.DATE), None
    if isinstance(sql_type, Interval):
        return TypeSpec.of(AttributeType.DURATION), None
    if isinstance(sql_type, JSON | postgresql.JSONB):
        return TypeSpec.of(AttributeType.JSON), None
    if isinstance(sql_type, Uuid | postgresql.UUID):
        return TypeSpec.of(AttributeType.STRING), None
    if isinstance(sql_type, Enum):
        values = tuple(str(v) for v in sql_type.enums)
        return (TypeSpec.enum(*values), None) if values else (None, None)
    if isinstance(sql_type, Text):
        return TypeSpec.of(AttributeType.TEXT), None
    if isinstance(sql_type, String):
        return TypeSpec.of(AttributeType.STRING), sql_type.length
    if isinstance(sql_type, ARRAY):
        element, _ = type_spec(sql_type.item_type)
        if element is not None and element.name not in (AttributeType.ARRAY, AttributeType.JSON):
            return TypeSpec.array(element), None
        return None, None
    return None, None


def _reflect(
    connection: Connection, schema: str, names: Sequence[str] | None, max_tables: int
) -> list[_Table]:
    inspector = inspect(connection)
    available = sorted(inspector.get_table_names(schema=schema))
    if names:
        missing = sorted(set(names) - set(available))
        if missing:
            raise PostgresProfileError(f"unknown tables in schema {schema!r}: {', '.join(missing)}")
        chosen = [n for n in available if n in set(names)]
    else:
        chosen = available
    if len(chosen) > max_tables:
        raise PostgresProfileError(
            f"{len(chosen)} tables requested, more than the {max_tables} allowed"
        )
    tables = []
    for name in chosen:
        primary = tuple(
            inspector.get_pk_constraint(name, schema=schema).get("constrained_columns") or ()
        )
        unique = {
            u["column_names"][0]
            for u in inspector.get_unique_constraints(name, schema=schema)
            if len(u["column_names"]) == 1
        }
        columns = []
        for info in inspector.get_columns(name, schema=schema):
            spec, max_length = type_spec(info["type"])
            columns.append(
                _Column(
                    name=info["name"],
                    spec=spec,
                    max_length=max_length,
                    nullable=bool(info.get("nullable", True)) and info["name"] not in primary,
                    unique=info["name"] in unique,
                    comment=info.get("comment"),
                )
            )
        foreign_keys = tuple(
            DeclaredForeignKey(
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                tuple(fk["referred_columns"]),
            )
            for fk in inspector.get_foreign_keys(name, schema=schema)
            if fk.get("referred_schema") in (None, schema)
        )
        comment = inspector.get_table_comment(name, schema=schema).get("text")
        tables.append(_Table(name, tuple(columns), primary, foreign_keys, comment))
    return tables


async def _sample(
    connection: AsyncConnection, schema: str, found: _Table, settings: PostgresProfilingSettings
) -> TableProfile:
    source = table(found.name, *(column(c.name) for c in found.columns), schema=schema)
    query = select(*source.columns).limit(settings.sample_rows)
    rows = (await connection.execute(query)).all()
    count: int | None
    if settings.exact_row_count:
        count = (await connection.execute(select(func.count()).select_from(source))).scalar_one()
    else:
        estimate = (
            await connection.execute(_ESTIMATE, {"schema": schema, "table": found.name})
        ).scalar()
        count = int(estimate) if estimate is not None and estimate >= 0 else None
    columns = tuple(
        ColumnProfile(
            name=c.name,
            values=[row[index] for row in rows],
            declared_type=c.spec,
            declared_max_length=c.max_length,
            nullable=c.nullable,
            unique=c.unique or None,
            comment=c.comment,
        )
        for index, c in enumerate(found.columns)
    )
    return TableProfile(
        name=found.name,
        columns=columns,
        row_count=max(count or 0, len(rows)),
        primary_key=found.primary_key or None,
        foreign_keys=found.foreign_keys,
        comment=found.comment,
    )


async def profile_postgres(
    engine: AsyncEngine,
    settings: PostgresProfilingSettings,
    *,
    schema: str = DEFAULT_SCHEMA,
    tables: Sequence[str] | None = None,
) -> list[TableProfile]:
    """Profiles of ``tables`` (default: every table) of ``schema``, read-only."""
    async with engine.connect() as connection, connection.begin():
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        await connection.execute(
            text(f"SET LOCAL statement_timeout = {int(settings.statement_timeout_ms)}")
        )
        found = await connection.run_sync(_reflect, schema, tables, settings.max_tables)
        return [await _sample(connection, schema, t, settings) for t in found]
