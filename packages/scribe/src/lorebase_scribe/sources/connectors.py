"""Readers of Source rows (design 9.1): CSV files or URLs, Postgres tables and queries.

A connector yields `(locator, row)` pairs; the locator (`name:row number`) becomes the row
locator of the provenance entries. The connection of a Source is a secret reference; its
value is the CSV location or the Postgres DSN.

- CSV: a local path under one of `scribe.sources.csv.allowed_roots`, or an `http(s)` URL whose
  scheme and host are allowed (`allowed_schemes`, `allowed_hosts`). Downloads never follow
  redirects and stop at `max_bytes`. The delimiter is sniffed, the first decodable encoding of
  `encodings` wins, and the first row is the header.
- Postgres: the `table` (default: the last segment of the Source api name) or the `query` of
  the Source, filtered by `scope_filter`, read in one read-only transaction with a statement
  timeout and fetched `fetch_rows` at a time.

Incremental Sources read only rows whose watermark column is greater than the highest watermark
of the previous successful run (compared in SQL with the column's own type for Postgres, as text
for CSV, so CSV watermarks should be ISO timestamps or zero-padded numbers).
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from lorebase_core.model import Source
from lorebase_core.model.source import SourceType
from lorebase_platform.secrets import SecretResolutionError, resolve_secret
from lorebase_scribe.settings import CsvSourceSettings, PostgresSourceSettings, SourceSettings
from lorebase_scribe.sources.mapping import Row, SourceDefinitionError
from lorebase_store.db import async_url

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
APPLICATION_NAME = "lorebase-scribe"


class SourceReadError(RuntimeError):
    """The Source could not be read (network, file system or database); a retry may work."""


class Connector(Protocol):
    """Reads the rows of one Source."""

    @property
    def columns(self) -> tuple[str, ...]:
        """Column names, known after the first row was read (header for CSV)."""
        ...

    def rows(self, since: Any) -> AsyncIterator[tuple[str, Row]]:
        """Rows in source order; with `since`, only rows whose watermark is greater."""
        ...


ConnectorFactory = Callable[[Source, SourceSettings], Connector]


def _location(source: Source) -> str:
    try:
        return resolve_secret(source.connection)
    except SecretResolutionError as error:
        raise SourceDefinitionError(f"connection: {error}") from None


def quote_identifier(name: str) -> str:
    """A dotted SQL identifier, each part checked and double-quoted."""
    parts = name.split(".")
    if not parts or any(_IDENTIFIER.fullmatch(part) is None for part in parts):
        raise SourceDefinitionError(f"not a plain SQL identifier: {name!r}")
    return ".".join(f'"{part}"' for part in parts)


# CSV ----------------------------------------------------------------------------------------


@dataclass
class CsvConnector:
    source: Source
    settings: CsvSourceSettings
    transport: httpx.AsyncBaseTransport | None = None
    _columns: tuple[str, ...] = ()

    @property
    def columns(self) -> tuple[str, ...]:
        return self._columns

    async def _load(self) -> tuple[str, bytes]:
        location = _location(self.source)
        parts = urlsplit(location)
        if parts.scheme in ("http", "https"):
            return Path(parts.path).name or parts.netloc, await self._download(location)
        if parts.scheme not in ("", "file"):
            raise SourceDefinitionError(f"unsupported CSV location scheme {parts.scheme!r}")
        return self._read_file(Path(parts.path if parts.scheme == "file" else location))

    def _read_file(self, path: Path) -> tuple[str, bytes]:
        resolved = path.resolve()
        roots = [Path(root).resolve() for root in self.settings.allowed_roots]
        if not any(resolved.is_relative_to(root) for root in roots):
            raise SourceDefinitionError(f"{path} is outside scribe.sources.csv.allowed_roots")
        try:
            size = resolved.stat().st_size
            if size > self.settings.max_bytes:
                raise SourceDefinitionError(
                    f"{path.name}: {size} bytes, more than the {self.settings.max_bytes} allowed"
                )
            return path.name, resolved.read_bytes()
        except OSError as error:
            raise SourceReadError(f"{path.name}: {error.strerror}") from None

    async def _download(self, url: str) -> bytes:
        parts = urlsplit(url)
        if parts.scheme not in self.settings.allowed_schemes:
            raise SourceDefinitionError(
                f"URL scheme {parts.scheme!r} is not in scribe.sources.csv.allowed_schemes"
            )
        if (parts.hostname or "") not in self.settings.allowed_hosts:
            raise SourceDefinitionError(
                f"host {parts.hostname!r} is not in scribe.sources.csv.allowed_hosts"
            )
        chunks: list[bytes] = []
        size = 0
        try:
            async with (
                httpx.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                    timeout=self.settings.timeout_s,
                    transport=self.transport,
                ) as client,
                client.stream("GET", url) as response,
            ):
                if response.status_code != 200:
                    raise SourceReadError(f"GET {parts.hostname}: HTTP {response.status_code}")
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.settings.max_bytes:
                        raise SourceDefinitionError(
                            f"download larger than the {self.settings.max_bytes} bytes allowed"
                        )
                    chunks.append(chunk)
        except httpx.HTTPError as error:
            raise SourceReadError(f"GET {parts.hostname}: {type(error).__name__}") from None
        return b"".join(chunks)

    def _decode(self, name: str, data: bytes) -> str:
        for encoding in self.settings.encodings:
            try:
                return data.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        raise SourceDefinitionError(
            f"{name}: not decodable as {', '.join(self.settings.encodings)}"
        )

    def _delimiter(self, sample: str, name: str) -> str:
        if name.lower().endswith((".tsv", ".tab")):
            return "\t"
        try:
            return csv.Sniffer().sniff(sample, delimiters=self.settings.delimiters).delimiter
        except csv.Error:
            return ","

    async def rows(self, since: Any) -> AsyncIterator[tuple[str, Row]]:
        name, data = await self._load()
        content = self._decode(name, data)
        delimiter = self._delimiter(content[: self.settings.sniff_bytes], name)
        reader = csv.reader(io.StringIO(content, newline=""), delimiter=delimiter)
        try:
            header = next(reader, None)
            if header is None:
                raise SourceDefinitionError(f"{name}: the file is empty")
            self._columns = tuple(cell.strip() for cell in header)
            watermark = self.source.watermark_column
            for number, cells in enumerate(reader, start=2):
                if not any(cell.strip() for cell in cells):
                    continue
                row = {
                    c: (cells[i] if i < len(cells) else None) for i, c in enumerate(self._columns)
                }
                if since is not None and watermark is not None:
                    value = row.get(watermark)
                    if value is None or value <= str(since):
                        continue
                yield f"{name}:{number}", row
        except csv.Error as error:
            raise SourceDefinitionError(f"{name}: not valid delimited text ({error})") from None


# Postgres -----------------------------------------------------------------------------------


@dataclass
class PostgresConnector:
    source: Source
    settings: PostgresSourceSettings
    _columns: tuple[str, ...] = ()

    @property
    def columns(self) -> tuple[str, ...]:
        return self._columns

    def statement(self, since: Any) -> tuple[str, dict[str, Any], str]:
        """SQL text, parameters and the relation name used in row locators."""
        source = self.source
        if source.type is SourceType.POSTGRES_QUERY:
            if not (source.query or "").strip():
                raise SourceDefinitionError("postgres_query Sources need a query")
            body = f"SELECT * FROM ({source.query.strip().rstrip(';')}) AS lorebase_source"  # type: ignore[union-attr]
            name = "query"
        else:
            name = source.table or source.api_name.rsplit(".", 1)[-1]
            body = f"SELECT * FROM {quote_identifier(name)} AS lorebase_source"
        conditions: list[str] = []
        parameters: dict[str, Any] = {}
        if source.scope_filter:
            conditions.append(f"({source.scope_filter})")
        order = ""
        if source.watermark_column is not None:
            column = quote_identifier(source.watermark_column)
            if since is not None:
                conditions.append(f"{column} > :since")
                parameters["since"] = since
            order = f" ORDER BY {column}"
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        return body + where + order, parameters, name

    async def rows(self, since: Any) -> AsyncIterator[tuple[str, Row]]:
        sql, parameters, name = self.statement(since)
        try:
            url = async_url(_location(self.source))
        except ValueError as error:
            raise SourceDefinitionError(f"connection: {error}") from None
        engine = create_async_engine(
            url,
            poolclass=NullPool,
            connect_args={
                "timeout": self.settings.connect_timeout_s,
                "server_settings": {"application_name": APPLICATION_NAME},
            },
        )
        try:
            async with engine.connect() as connection, connection.begin():
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                await connection.execute(
                    text(f"SET LOCAL statement_timeout = {int(self.settings.statement_timeout_ms)}")
                )
                result = await connection.stream(text(sql), parameters)
                self._columns = tuple(result.keys())
                number = 0
                async for partition in result.mappings().partitions(self.settings.fetch_rows):
                    for row in partition:
                        number += 1
                        yield f"{name}:{number}", dict(row)
        except (OSError, TimeoutError) as error:
            raise SourceReadError(f"{name}: {type(error).__name__}: {error}") from None
        finally:
            await engine.dispose()


def default_connector(source: Source, settings: SourceSettings) -> Connector:
    """The connector of a Source type; Parquet and HTTP JSON are not supported yet."""
    if source.type is SourceType.CSV:
        return CsvConnector(source, settings.csv)
    if source.type in (SourceType.POSTGRES_TABLE, SourceType.POSTGRES_QUERY):
        return PostgresConnector(source, settings.postgres)
    raise SourceDefinitionError(f"Source type {source.type.value!r} is not supported yet")
