"""MySQL, MariaDB and SQL Server Sources: the `table` or `query` of the Source, filtered by
`scope_filter`, read like a Postgres Source (see `connectors`) and fetched `fetch_rows` at a
time. Locators are `table:<row number>` (the table name), or `query:<row number>`.

- `mysql_table`, `mysql_query`: MySQL 5.7+ and MariaDB 10.x through asyncmy (Apache-2.0, async,
  prebuilt wheels), installed with `lorebase-scribe[mysql]`. The session is read-only and
  bounded by `max_execution_time` (`max_statement_time` on MariaDB). The connection value is
  `mysql://user:password@host:3306/database` (`mariadb://` works too).
- `mssql_table`, `mssql_query`: SQL Server 2017+ and Azure SQL through pymssql (LGPL-2.1,
  FreeTDS bundled in the wheels, so no ODBC driver is needed), installed with
  `lorebase-scribe[mssql]`. pymssql is blocking: every driver call runs in a worker thread.
  SQL Server has no read-only transaction, so the login must only be able to read. The
  connection value is `mssql://user:password@host:1433/database`.

A `query` is wrapped as `SELECT * FROM (<query>) AS lorebase_source` and a trailing `;` is
removed. SQL Server refuses an `ORDER BY` in a derived table unless it has `TOP` or `OFFSET`, so
a query of that kind whose top level ends in `ORDER BY` without either gets `OFFSET 0 ROWS`
appended (valid from SQL Server 2012, and it does not limit the rows).
"""

from __future__ import annotations

import asyncio
import importlib
import math
import re
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import URL, create_engine, make_url, text
from sqlalchemy.exc import ArgumentError, DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from lorebase_core.model import Source
from lorebase_core.model.source import SourceType
from lorebase_scribe.sources.access import SourceReadError, resolve_location
from lorebase_scribe.sources.mapping import Row, SourceDefinitionError

__all__ = ["MssqlConnector", "MysqlConnector", "quote_identifier"]

APPLICATION_NAME = "lorebase-scribe"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_MAX_ERROR_CHARS = 300


def quote_identifier(name: str, opening: str = '"', closing: str = '"') -> str:
    """A dotted SQL identifier, each part checked and quoted."""
    parts = name.split(".")
    if not parts or any(_IDENTIFIER.fullmatch(part) is None for part in parts):
        raise SourceDefinitionError(f"not a plain SQL identifier: {name!r}")
    return ".".join(f"{opening}{part}{closing}" for part in parts)


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$#@]*")
_OPAQUE = re.compile(
    r"'(?:[^']|'')*'|\[(?:[^\]]|\]\])*\]|\"(?:[^\"]|\"\")*\"|--[^\n]*|/\*.*?\*/", re.S
)


def top_level_words(query: str) -> list[str]:
    """Upper-cased keywords of `query` outside parentheses, string literals, quoted
    identifiers and comments."""
    words: list[str] = []
    depth = 0
    index = 0
    while index < len(query):
        quoted = _OPAQUE.match(query, index)
        if quoted is not None:
            index = quoted.end()
            continue
        char = query[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and (word := _WORD.match(query, index)) is not None:
            words.append(word.group().upper())
            index = word.end()
            continue
        index += 1
    return words


def mssql_orderable(query: str) -> str:
    """`query` made valid as a derived table: a top-level trailing `ORDER BY` with no `TOP`,
    `OFFSET` or `FOR` clause gets `OFFSET 0 ROWS`, which keeps the order and all the rows."""
    words = top_level_words(query)
    if "TOP" in words or "OFFSET" in words or "FOR" in words:
        return query
    for position in range(len(words) - 1, 0, -1):
        if words[position] == "BY" and words[position - 1] == "ORDER":
            return f"{query}\nOFFSET 0 ROWS"
    return query


def _require_driver(module: str, extra: str, label: str) -> None:
    try:
        importlib.import_module(module)
    except ImportError:
        raise SourceDefinitionError(
            f"{label} Sources need {module}: install lorebase-scribe[{extra}]"
        ) from None


def _driver_url(location: str, schemes: frozenset[str], driver: str, hint: str) -> URL:
    """The URL of `location` on `driver`; refuses other schemes without echoing the value."""
    try:
        parsed = make_url(location)
    except ArgumentError:
        raise SourceDefinitionError("connection: not a valid database URL") from None
    if parsed.drivername not in schemes:
        raise SourceDefinitionError(
            f"connection: unsupported database driver {parsed.drivername!r}; use {hint}://"
        )
    return parsed.set(drivername=driver)


@contextmanager
def _read_errors(name: str) -> Iterator[None]:
    """Driver and network failures as `SourceReadError`, naming no connection value."""
    try:
        yield
    except DBAPIError as error:
        detail = " ".join(str(error.orig).split())[:_MAX_ERROR_CHARS]
        raise SourceReadError(f"{name}: {type(error.orig).__name__}: {detail}") from None
    except (OSError, TimeoutError) as error:
        raise SourceReadError(f"{name}: {type(error).__name__}: {error}") from None


class _SqlConnector:
    """Statement building shared by the SQL connectors; subclasses read."""

    #: `mysql` or `mssql`: the URL scheme to use, the extra to install, the settings name.
    label: ClassVar[str]
    query_type: ClassVar[SourceType]
    opening: ClassVar[str]
    closing: ClassVar[str]
    schemes: ClassVar[frozenset[str]]
    driver: ClassVar[str]
    #: Replaced by tests to read through a fake engine.
    engine_factory: ClassVar[Callable[..., Any]]

    def __init__(self, source: Source, settings: Any) -> None:
        self.source = source
        self.settings = settings
        self._columns: tuple[str, ...] = ()

    @property
    def columns(self) -> tuple[str, ...]:
        return self._columns

    def _quote(self, name: str) -> str:
        return quote_identifier(name, self.opening, self.closing)

    def _derived(self, query: str) -> str:
        """The query as the body of a derived table (unchanged unless a dialect needs more)."""
        return query

    def statement(self, since: Any) -> tuple[str, dict[str, Any], str]:
        """SQL text, parameters and the relation name used in row locators."""
        source = self.source
        if source.type is self.query_type:
            query = (source.query or "").strip().rstrip(";").rstrip()
            if not query:
                raise SourceDefinitionError(f"{self.query_type.value} Sources need a query")
            body = f"SELECT * FROM ({self._derived(query)}) AS lorebase_source"
            name = "query"
        else:
            name = source.table or source.api_name.rsplit(".", 1)[-1]
            body = f"SELECT * FROM {self._quote(name)} AS lorebase_source"
        conditions: list[str] = []
        parameters: dict[str, Any] = {}
        if source.scope_filter:
            conditions.append(f"({source.scope_filter})")
        order = ""
        if source.watermark_column is not None:
            column = self._quote(source.watermark_column)
            if since is not None:
                conditions.append(f"{column} > :since")
                parameters["since"] = since
            order = f" ORDER BY {column}"
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        return body + where + order, parameters, name

    def _url(self) -> URL:
        _require_driver(self.driver, self.label, self.label)
        return _driver_url(
            resolve_location(self.source),
            self.schemes,
            f"{self.label}+{self.driver}",
            self.label,
        )


class MysqlConnector(_SqlConnector):
    """A MySQL or MariaDB table or query, streamed through a server-side cursor in a
    read-only session."""

    label = "mysql"
    query_type = SourceType.MYSQL_QUERY
    opening = closing = "`"
    driver = "asyncmy"
    schemes = frozenset({"mysql", "mariadb", "mysql+asyncmy", "mariadb+asyncmy"})
    engine_factory = staticmethod(create_async_engine)

    async def rows(self, since: Any) -> AsyncIterator[tuple[str, Row]]:
        sql, parameters, name = self.statement(since)
        url = self._url()
        settings = self.settings
        connect_args: dict[str, Any] = {
            "connect_timeout": settings.connect_timeout_s,
            "program_name": APPLICATION_NAME,
        }
        if "charset" not in url.query:
            connect_args["charset"] = "utf8mb4"
        engine = self.engine_factory(url, poolclass=NullPool, connect_args=connect_args)
        try:
            with _read_errors(name):
                async with engine.connect() as connection:
                    # Session-wide, so that it holds for the transaction below; the implicit
                    # transaction the first statement opens is ended before the read.
                    await connection.execute(text("SET SESSION TRANSACTION READ ONLY"))
                    timeout = int(settings.statement_timeout_ms)
                    if getattr(connection.dialect, "is_mariadb", False):
                        seconds = math.ceil(timeout / 1000)
                        limit = f"SET SESSION max_statement_time = {seconds}"
                    else:
                        limit = f"SET SESSION max_execution_time = {timeout}"
                    await connection.execute(text(limit))
                    await connection.rollback()
                    async with connection.begin():
                        result = await connection.stream(text(sql), parameters)
                        self._columns = tuple(result.keys())
                        number = 0
                        async for partition in result.mappings().partitions(settings.fetch_rows):
                            for row in partition:
                                number += 1
                                yield f"{name}:{number}", dict(row)
        finally:
            await engine.dispose()


class MssqlConnector(_SqlConnector):
    """A SQL Server table or query; the blocking driver runs in worker threads."""

    label = "mssql"
    query_type = SourceType.MSSQL_QUERY
    opening, closing = "[", "]"
    driver = "pymssql"
    schemes = frozenset({"mssql", "sqlserver", "mssql+pymssql"})
    engine_factory = staticmethod(lambda url, **options: create_engine(url, **options))

    def _derived(self, query: str) -> str:
        return mssql_orderable(query)

    def statement(self, since: Any) -> tuple[str, dict[str, Any], str]:
        sql, parameters, name = super().statement(since)
        if isinstance(parameters.get("since"), datetime):
            # pymssql binds a datetime with millisecond precision; SQL Server converts the
            # text to the type of the watermark column, microseconds included.
            parameters["since"] = parameters["since"].isoformat(sep=" ")
        return sql, parameters, name

    async def rows(self, since: Any) -> AsyncIterator[tuple[str, Row]]:
        sql, parameters, name = self.statement(since)
        url = self._url()
        settings = self.settings
        timeout_ms = int(settings.statement_timeout_ms)
        engine = self.engine_factory(
            url,
            poolclass=NullPool,
            connect_args={
                "login_timeout": math.ceil(settings.connect_timeout_s),
                "timeout": math.ceil(timeout_ms / 1000),
                "appname": APPLICATION_NAME,
                # datetime2 keeps its microseconds: a watermark must not lose precision.
                "use_datetime2": True,
            },
        )
        connection: Any = None
        try:
            with _read_errors(name):
                connection = await asyncio.to_thread(engine.connect)
                await asyncio.to_thread(connection.execute, text(f"SET LOCK_TIMEOUT {timeout_ms}"))
                result = await asyncio.to_thread(connection.execute, text(sql), parameters)
                self._columns = tuple(result.keys())
                rows = result.mappings()
                number = 0
                while batch := await asyncio.to_thread(rows.fetchmany, settings.fetch_rows):
                    for row in batch:
                        number += 1
                        yield f"{name}:{number}", dict(row)
        finally:
            await asyncio.shield(asyncio.to_thread(self._close, engine, connection))

    @staticmethod
    def _close(engine: Any, connection: Any) -> None:
        try:
            if connection is not None:
                connection.close()
        finally:
            engine.dispose()
