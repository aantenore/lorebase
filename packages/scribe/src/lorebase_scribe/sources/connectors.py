"""Readers of Source rows (design 9.1): CSV files or URLs, Postgres tables and queries.

A connector yields `(locator, row)` pairs; the locator (`name:row number`) becomes the row
locator of the provenance entries. The connection of a Source is a secret reference; its
value is the CSV location or the Postgres DSN.

- CSV: a local path under one of `scribe.sources.csv.allowed_roots`, or an `http(s)` URL whose
  scheme and host are allowed (`allowed_schemes`, `allowed_hosts`). A local path is resolved
  (symbolic links included) and must then lie under a resolved root; the file is opened from
  the root one component at a time without following symbolic links, so a link swapped in
  after the check is refused instead of followed, and it must be a regular file. A URL host
  must resolve to global unicast addresses only (unless `allow_private_targets`), checked as
  for webhooks, and the download connects to those checked addresses, never to a new lookup.
  Downloads never follow redirects; files and downloads are read in bounded steps and refused
  past `max_bytes`. The delimiter is sniffed, the first decodable encoding of `encodings`
  wins, and the first row is the header.
- Postgres: the `table` (default: the last segment of the Source api name) or the `query` of
  the Source, filtered by `scope_filter`, read in one read-only transaction with a statement
  timeout and fetched `fetch_rows` at a time.

Incremental Sources read only rows whose watermark column is greater than the highest watermark
of the previous successful run (compared in SQL with the column's own type for Postgres, as text
for CSV, so CSV watermarks should be ISO timestamps or zero-padded numbers).
"""

from __future__ import annotations

import asyncio
import csv
import errno
import io
import os
import re
import stat
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath
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
from lorebase_scribe.webhooks import pin
from lorebase_store.db import async_url
from lorebase_store.webhooks import WebhookSettings
from lorebase_store.webhooks.delivery import Resolver, check_target, resolve_host

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
APPLICATION_NAME = "lorebase-scribe"
READ_CHUNK = 1024 * 1024
"""Bytes read from a local CSV file per call; the total is bounded by `max_bytes`."""


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


def check_connection(source: Source, settings: SourceSettings) -> None:
    """Refuse a Source whose connection is not an allowed secret reference.

    Sources come from the schema, written by API callers; resolving any reference they name
    would let them read whatever the host can reach with it (the Lorebase database DSN
    included). Messages here and below name the reference at most, never its value.
    """
    if source.connection not in settings.allowed_connections:
        raise SourceDefinitionError(
            f"connection {source.connection!r} is not in scribe.sources.allowed_connections"
        )


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


def _open_beneath(root: Path, relative: PurePath) -> int:
    """A descriptor of `root / relative`, opened without following any symbolic link.

    Each directory below `root` is opened relative to its parent with `O_NOFOLLOW`, and so is
    the file itself (non-blocking, so a FIFO cannot hold the worker): a component replaced by
    a symbolic link after the path was checked fails with ELOOP or ENOTDIR.
    """
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(
            relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    finally:
        os.close(directory)


@dataclass
class CsvConnector:
    source: Source
    settings: CsvSourceSettings
    transport: httpx.AsyncBaseTransport | None = None
    resolve: Resolver = resolve_host
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
            raise SourceDefinitionError("connection: unsupported CSV location scheme")
        return self._read_file(Path(parts.path if parts.scheme == "file" else location))

    def _beneath_root(self, path: Path) -> tuple[Path, PurePath]:
        """The allowed root `path` resolves under, and the resolved path relative to it.

        Symbolic links are resolved here, so a link inside a root that points inside a root
        keeps working; the file is then opened along the resolved path, which has no links,
        without following any (`_open_beneath`)."""
        try:
            resolved = path.resolve()
            roots = [Path(root).resolve() for root in self.settings.allowed_roots]
        except (OSError, RuntimeError):
            raise SourceReadError(f"{path.name}: the path cannot be resolved") from None
        for root in roots:
            if resolved != root and resolved.is_relative_to(root):
                return root, resolved.relative_to(root)
        raise SourceDefinitionError(
            "connection: the path is outside scribe.sources.csv.allowed_roots"
        )

    def _read_file(self, path: Path) -> tuple[str, bytes]:
        root, relative = self._beneath_root(path)
        try:
            descriptor = _open_beneath(root, relative)
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENOTDIR):
                raise SourceDefinitionError(
                    "connection: the path meets a symbolic link or a non-directory when opened "
                    "under scribe.sources.csv.allowed_roots"
                ) from None
            raise SourceReadError(f"{path.name}: {error.strerror}") from None
        try:
            return path.name, self._read_descriptor(path.name, descriptor)
        except OSError as error:
            raise SourceReadError(f"{path.name}: {error.strerror}") from None
        finally:
            os.close(descriptor)

    def _read_descriptor(self, name: str, descriptor: int) -> bytes:
        """At most `max_bytes` of a regular file, read from the descriptor that was checked."""
        limit = self.settings.max_bytes
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise SourceDefinitionError(f"{name}: not a regular file")
        if status.st_size > limit:
            raise SourceDefinitionError(
                f"{name}: {status.st_size} bytes, more than the {limit} allowed by "
                "scribe.sources.csv.max_bytes"
            )
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(descriptor, min(READ_CHUNK, limit + 1 - size)):
            size += len(chunk)
            if size > limit:  # the file grew after fstat
                raise SourceDefinitionError(
                    f"{name}: more than the {limit} bytes allowed by scribe.sources.csv.max_bytes"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    async def _download(self, url: str) -> bytes:
        parts = urlsplit(url)
        if parts.scheme not in self.settings.allowed_schemes:
            raise SourceDefinitionError(
                "connection: the URL scheme is not in scribe.sources.csv.allowed_schemes"
            )
        if (parts.hostname or "") not in self.settings.allowed_hosts:
            raise SourceDefinitionError(
                "connection: the URL host is not in scribe.sources.csv.allowed_hosts"
            )
        try:
            async with (
                asyncio.timeout(self.settings.timeout_s),
                httpx.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                    timeout=self.settings.timeout_s,
                    transport=self.transport,
                ) as client,
            ):
                addresses = await self._addresses(url)
                return await self._fetch(client, httpx.URL(url), addresses)
        except TimeoutError:
            raise SourceReadError(
                f"GET {parts.hostname}: not complete within scribe.sources.csv.timeout_s"
            ) from None
        except httpx.HTTPError as error:
            raise SourceReadError(f"GET {parts.hostname}: {type(error).__name__}") from None

    async def _addresses(self, url: str) -> tuple[str, ...]:
        """The addresses of the URL host, checked as the webhook worker checks its targets.

        Empty when private targets are allowed: the client then resolves the host itself."""
        checks = WebhookSettings(
            allowed_schemes=self.settings.allowed_schemes,
            allow_private_targets=self.settings.allow_private_targets,
        )
        target = await check_target(url, checks, self.resolve)
        if target.problem is None:
            return target.addresses
        if target.problem.startswith("cannot resolve"):
            raise SourceReadError("connection: the URL host cannot be resolved")
        # `check_target` problems never contain the host name or the URL.
        raise SourceDefinitionError(
            f"connection: the URL host {target.problem}, refused unless "
            "scribe.sources.csv.allow_private_targets"
        )

    async def _fetch(
        self, client: httpx.AsyncClient, url: httpx.URL, addresses: Sequence[str]
    ) -> bytes:
        if not addresses:
            return await self._get(client, url, url, {}, {})
        failure: httpx.TransportError | None = None
        for address in addresses:
            pinned, host, extensions = pin(url, address)
            try:
                return await self._get(client, url, pinned, host, extensions)
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                failure = error  # the next checked address, never a new lookup
        assert failure is not None
        raise failure

    async def _get(
        self,
        client: httpx.AsyncClient,
        url: httpx.URL,
        target: httpx.URL,
        headers: dict[str, str],
        extensions: dict[str, Any],
    ) -> bytes:
        """The body of a GET to `target` (`url`, maybe pinned), at most `max_bytes` of it."""
        limit = self.settings.max_bytes
        chunks: list[bytes] = []
        size = 0
        async with client.stream("GET", target, headers=headers, extensions=extensions) as response:
            if response.status_code != 200:
                raise SourceReadError(f"GET {url.host}: HTTP {response.status_code}")
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise SourceDefinitionError(
                        f"download larger than the {limit} bytes allowed by "
                        "scribe.sources.csv.max_bytes"
                    )
                chunks.append(chunk)
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
    """The connector of a Source type; Parquet and HTTP JSON are not supported yet.

    The connection must be one of `settings.allowed_connections`."""
    check_connection(source, settings)
    if source.type is SourceType.CSV:
        return CsvConnector(source, settings.csv)
    if source.type in (SourceType.POSTGRES_TABLE, SourceType.POSTGRES_QUERY):
        return PostgresConnector(source, settings.postgres)
    raise SourceDefinitionError(f"Source type {source.type.value!r} is not supported yet")
