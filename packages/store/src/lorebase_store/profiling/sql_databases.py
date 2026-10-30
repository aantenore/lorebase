"""MySQL, MariaDB and SQL Server profiler: catalog reflection (SQLAlchemy) plus a bounded
SELECT per table, profiled as records (``records.RecordProfiler``).

The sample is ``sample_rows`` rows (``LIMIT`` or ``TOP``, from the dialect); with ``count_rows``
each table is also counted, so inference knows when the sample is partial. Declared primary
keys and foreign keys come from the catalog. MySQL sessions are read-only and bounded by
``max_execution_time`` (``max_statement_time`` on MariaDB); SQL Server has no read-only
transaction, so the login must only be able to read (its waits are bounded by the driver
timeout and ``LOCK_TIMEOUT``). MySQL runs through asyncmy; pymssql is blocking and runs in a
worker thread. The drivers are optional extras of the installing package, which the caller
names as ``application`` (``SqlDriverError`` names the extra when missing; the connection
reports the same name to the server). Binary values (a ``rowversion``) are profiled as hex
text. ``SQL_DIALECTS`` lists the URL schemes, drivers and extras of each dialect, for every
package that reads these databases.
"""

from __future__ import annotations

import asyncio
import importlib
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from sqlalchemy import (
    URL,
    ColumnElement,
    Connection,
    column,
    create_engine,
    func,
    inspect,
    make_url,
    select,
    text,
)
from sqlalchemy import table as sql_table
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.types import BINARY, VARBINARY, LargeBinary, NullType, String, TypeEngine

from lorebase_core.infer import DeclaredForeignKey, TableProfile
from lorebase_store.profiling.records import RecordProfileError, RecordProfiler
from lorebase_store.settings import RecordProfilingSettings, SqlProfilingSettings

SqlDialect = Literal["mysql", "mssql"]


class SqlProfileError(ValueError):
    """The requested tables cannot be profiled."""


class SqlDriverError(SqlProfileError):
    """The driver of the database is not installed."""


@dataclass(frozen=True)
class SqlDialectSpec:
    """How to reach one dialect: the URL schemes accepted, the SQLAlchemy driver name they
    are rewritten to, the driver module and the package extra that installs it."""

    schemes: frozenset[str]
    driver_name: str
    module: str
    extra: str


SQL_DIALECTS: Mapping[SqlDialect, SqlDialectSpec] = {
    "mysql": SqlDialectSpec(
        frozenset({"mysql", "mariadb", "mysql+asyncmy", "mariadb+asyncmy"}),
        "mysql+asyncmy",
        "asyncmy",
        "mysql",
    ),
    "mssql": SqlDialectSpec(
        frozenset({"mssql", "sqlserver", "mssql+pymssql"}), "mssql+pymssql", "pymssql", "mssql"
    ),
}


def _url(dialect: SqlDialect, location: str, application: str) -> URL:
    """The URL on the driver of `dialect`; refuses other schemes without echoing the value."""
    found = SQL_DIALECTS[dialect]
    try:
        importlib.import_module(found.module)
    except ImportError:
        raise SqlDriverError(
            f"{dialect} sources need {found.module}: install {application}[{found.extra}]"
        ) from None
    try:
        parsed = make_url(location)
    except ArgumentError:
        raise SqlProfileError("the connection is not a valid database URL") from None
    if parsed.drivername not in found.schemes:
        raise SqlProfileError(f"the connection must be a {dialect}:// URL")
    return parsed.set(drivername=found.driver_name)


def _plain(value: Any) -> Any:
    """Binary values (a rowversion) as hex text, the rest unchanged."""
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).hex()
    return value


def _check_schema(
    inspector: Any, schema: str | None, allowed: Sequence[str] | None, max_chars: int = 128
) -> None:
    """Refuse a schema that is not a plain name of this database's catalog, or that is not
    among the `allowed` ones (the connection's own schema when none is named)."""
    if schema is not None:
        if not schema or len(schema) > max_chars or "." in schema or not schema.isprintable():
            raise SqlProfileError("the schema is not a plain schema name")
        if schema not in inspector.get_schema_names():
            raise SqlProfileError(f"unknown schema {schema!r}")
    if allowed is not None:
        effective = schema if schema is not None else inspector.default_schema_name
        if effective not in allowed:
            raise SqlProfileError("the schema is not allowed for this source")


def _readable(column_name: str, sql_type: TypeEngine[Any], source: Any, limit: int) -> Any:
    """The select item for a column, bounded: long or unbounded text is cut to `limit`
    characters, binary columns without a small declared length and columns of unknown type
    are not read (`None`)."""
    found = source.c[column_name]
    if isinstance(sql_type, NullType):
        return None
    if isinstance(sql_type, LargeBinary | BINARY | VARBINARY):
        length = getattr(sql_type, "length", None)
        return found if length is not None and length <= limit else None
    if isinstance(sql_type, String):
        length = sql_type.length
        if length is None or length > limit:
            return func.substring(found, 1, limit).label(column_name)
    return found


def _profile_tables(
    connection: Connection,
    schema: str | None,
    names: Sequence[str] | None,
    settings: SqlProfilingSettings,
    records: RecordProfilingSettings,
    allowed_schemas: Sequence[str] | None = None,
) -> list[TableProfile]:
    inspector = inspect(connection)
    _check_schema(inspector, schema, allowed_schemas)
    wanted = set(names or ())
    if len(wanted) > settings.max_tables:
        raise SqlProfileError(
            f"{len(wanted)} tables requested, more than the {settings.max_tables} allowed"
        )
    available = sorted(inspector.get_table_names(schema=schema))
    if wanted:
        missing = sorted(wanted - set(available))
        if missing:
            where = f"schema {schema!r}" if schema else "the database"
            raise SqlProfileError(f"unknown tables in {where}: {', '.join(missing)}")
        chosen = [name for name in available if name in wanted]
    else:
        chosen = available
    if len(chosen) > settings.max_tables:
        raise SqlProfileError(
            f"{len(chosen)} tables requested, more than the {settings.max_tables} allowed"
        )
    profiles = []
    deadline = time.monotonic() + settings.total_timeout_s
    for name in chosen:
        if time.monotonic() > deadline:
            raise SqlProfileError(
                f"reading the tables took longer than the {settings.total_timeout_s:g} s allowed"
            )
        info = inspector.get_columns(name, schema=schema)
        source = sql_table(name, *(column(str(item["name"])) for item in info), schema=schema)
        limit = settings.max_value_chars
        items = [
            (str(item["name"]), _readable(str(item["name"]), item["type"], source, limit))
            for item in info
        ]
        read: list[tuple[str, ColumnElement[Any]]] = [(n, e) for n, e in items if e is not None]
        if not read:
            continue  # nothing readable: no columns, or only unreadable ones
        columns = [n for n, _ in read]
        profiler = RecordProfiler(records)
        query = select(*(e for _, e in read)).select_from(source).limit(settings.sample_rows)
        for row in connection.execute(query):
            profiler.add({c: _plain(value) for c, value in zip(columns, row, strict=True)})
        try:
            profile = profiler.profile(name, columns)
        except RecordProfileError as error:
            raise SqlProfileError(f"{name}: {error}") from None
        sampled = profile.row_count or 0
        count = (
            connection.execute(select(func.count()).select_from(source)).scalar_one()
            if settings.count_rows
            else None
        )
        primary = tuple(
            inspector.get_pk_constraint(name, schema=schema).get("constrained_columns") or ()
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
        profiles.append(
            replace(
                profile,
                row_count=profile.row_count if count is None else max(int(count), sampled),
                primary_key=primary or None,
                foreign_keys=foreign_keys,
            )
        )
    return profiles


async def _profile_mysql(
    url: URL,
    settings: SqlProfilingSettings,
    run: Callable[[Connection], list[TableProfile]],
    engine_factory: Callable[..., Any],
    application: str,
) -> list[TableProfile]:
    connect_args: dict[str, Any] = {
        "connect_timeout": settings.connect_timeout_s,
        "program_name": application,
    }
    if "charset" not in url.query:
        connect_args["charset"] = "utf8mb4"
    engine = engine_factory(url, poolclass=NullPool, connect_args=connect_args)
    try:
        async with engine.connect() as connection:
            # Session-wide, so that it holds for the transaction the first query opens.
            await connection.execute(text("SET SESSION TRANSACTION READ ONLY"))
            timeout = int(settings.statement_timeout_ms)
            if getattr(connection.dialect, "is_mariadb", False):
                limit = f"SET SESSION max_statement_time = {math.ceil(timeout / 1000)}"
            else:
                limit = f"SET SESSION max_execution_time = {timeout}"
            await connection.execute(text(limit))
            await connection.rollback()
            return list(await connection.run_sync(run))
    finally:
        await engine.dispose()


def _mssql_work(
    url: URL,
    settings: SqlProfilingSettings,
    run: Callable[[Connection], list[TableProfile]],
    engine_factory: Callable[..., Any],
    application: str,
) -> list[TableProfile]:
    # pymssql cannot send ApplicationIntent=ReadOnly: the login must be able to read only.
    timeout_ms = int(settings.statement_timeout_ms)
    engine = engine_factory(
        url,
        poolclass=NullPool,
        connect_args={
            "login_timeout": math.ceil(settings.connect_timeout_s),
            "timeout": math.ceil(timeout_ms / 1000),
            "appname": application,
        },
    )
    try:
        with engine.connect() as connection:
            connection.execute(text(f"SET LOCK_TIMEOUT {timeout_ms}"))
            return run(connection)
    finally:
        engine.dispose()


async def profile_sql_database(
    dialect: SqlDialect,
    location: str,
    settings: SqlProfilingSettings,
    records: RecordProfilingSettings,
    *,
    application: str,
    schema: str | None = None,
    tables: Sequence[str] | None = None,
    allowed_schemas: Sequence[str] | None = None,
    engine_factory: Callable[..., Any] | None = None,
) -> list[TableProfile]:
    """Profiles of ``tables`` (default: every table) of a MySQL, MariaDB or SQL Server database
    named by the connection URL ``location``; ``schema`` defaults to the connection's own and
    must be a plain name found in the catalog (and in ``allowed_schemas`` when given; the
    connection's own schema is then checked too). ``application`` is the installing package:
    the application name the server sees and the package a missing driver's extra belongs
    to. ``engine_factory`` replaces the engine constructor (tests)."""
    url = _url(dialect, location, application)

    def run(connection: Connection) -> list[TableProfile]:
        return _profile_tables(connection, schema, tables, settings, records, allowed_schemas)

    if dialect == "mysql":
        return await _profile_mysql(
            url, settings, run, engine_factory or create_async_engine, application
        )
    return await asyncio.to_thread(
        _mssql_work, url, settings, run, engine_factory or create_engine, application
    )
