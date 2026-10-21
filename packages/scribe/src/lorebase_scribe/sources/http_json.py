"""Reader of `http_json` Sources: GET requests to a JSON API, page by page.

The connection's value is the URL of the first page. Every request, next pages included, goes
through the guards of `access` with the `scribe.sources.http_json` settings: an allowed scheme
and host, addresses checked as for webhooks (global unicast only unless
`allow_private_targets`) and looked up once per host and run, connections pinned to them, no
redirects, responses refused past `max_bytes`, `timeout_s` per request from lookup to last
byte.

- Headers come from the Source's `http.headers`. A value that is a secret reference is
  resolved like the connection, and only when it is listed in
  `scribe.sources.allowed_connections`; credential headers (`Authorization`, `X-Api-Key`...)
  accept nothing else, so no credential is ever stored in a schema.
- Records are the array at `records_path` of each response, or the response itself.
- Pagination (`http.pagination.type`): `none`; `cursor` (the value at `cursor_path` goes into
  `cursor_param` of the next request); `page` (`page_param` counts up from `start_page`);
  `offset` (`offset_param` moves by the records received); `link` (the `rel="next"` URL of the
  `Link` header). Reading stops at a page without records or without a next page; a run that
  would need more than `max_pages` pages (or the Source's lower `http.max_pages`) fails rather
  than commit part of the Source.
- 429 and 5xx answers (`retry_statuses`), network errors and timeouts are retried up to
  `max_attempts` times with exponential backoff, honoring `Retry-After` up to
  `backoff_max_s`. Other statuses fail the read.
- Incremental reads send the previous watermark as `http.since_param` when it is set, and keep
  only rows whose watermark column is greater either way.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import PurePosixPath
from typing import Any

import httpx

from lorebase_core.model import SECRET_REFERENCE_PREFIX, Source
from lorebase_core.model.source import (
    HttpOptions,
    HttpPagination,
    PaginationType,
    credential_header,
)
from lorebase_platform.secrets import SecretResolutionError, resolve_secret
from lorebase_scribe.settings import HttpJsonSourceSettings
from lorebase_scribe.sources.access import (
    Fetched,
    HttpRules,
    PinnedClient,
    SourceReadError,
    resolve_location,
)
from lorebase_scribe.sources.mapping import Row, SourceDefinitionError, text_of
from lorebase_scribe.sources.records import flatten, keep, records_at, value_at
from lorebase_store.webhooks.delivery import Resolver, resolve_host

SECTION = "scribe.sources.http_json"
ACCEPT = "application/json"
_LINK = re.compile(r"\s*<([^>]*)>(.*)")
_REL = re.compile(r"""\brel\s*=\s*("([^"]*)"|[^\s;]+)""", re.IGNORECASE)

Sleep = Callable[[float], Awaitable[None]]


def next_link(header: str | None) -> str | None:
    """The `rel="next"` target of a `Link` header (RFC 8288), or None."""
    for part in (header or "").split(","):
        found = _LINK.match(part)
        if found is None:
            continue
        for rel in _REL.finditer(found.group(2)):
            values = (rel.group(2) if rel.group(2) is not None else rel.group(1)).split()
            if "next" in (value.lower() for value in values):
                return found.group(1).strip()
    return None


def retry_after(headers: httpx.Headers, now: datetime) -> float | None:
    """Seconds a `Retry-After` header asks to wait (delay or HTTP date), or None."""
    value = headers.get("retry-after")
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - now).total_seconds(), 0.0)


class _Pages:
    """The request of each page."""

    def __init__(self, pagination: HttpPagination, url: httpx.URL) -> None:
        self.rule = pagination
        if pagination.size_param is not None and pagination.page_size is not None:
            url = url.copy_set_param(pagination.size_param, str(pagination.page_size))
        self.base = url
        self.index = 0
        self.offset = 0
        self.cursors: set[str] = set()

    def first(self) -> httpx.URL:
        if self.rule.type is PaginationType.PAGE:
            return self.base.copy_set_param(self.rule.page_param, str(self.rule.start_page))
        if self.rule.type is PaginationType.OFFSET:
            return self.base.copy_set_param(self.rule.offset_param, "0")
        return self.base

    def next(
        self, current: httpx.URL, fetched: Fetched, document: Any, count: int
    ) -> httpx.URL | None:
        """The next page's URL, or None after the last page."""
        rule = self.rule
        if rule.type is PaginationType.NONE or count == 0:
            return None
        self.index += 1
        if rule.type is PaginationType.PAGE:
            return self.base.copy_set_param(rule.page_param, str(rule.start_page + self.index))
        if rule.type is PaginationType.OFFSET:
            self.offset += count
            return self.base.copy_set_param(rule.offset_param, str(self.offset))
        if rule.type is PaginationType.LINK:
            target = next_link(fetched.headers.get("link"))
            if target is None:
                return None
            try:
                return current.join(target)
            except httpx.InvalidURL:
                raise SourceReadError("the next page link is not a valid URL") from None
        assert rule.cursor_path is not None
        try:
            cursor = value_at(document, rule.cursor_path)
        except KeyError:
            return None
        if cursor is None or cursor == "" or isinstance(cursor, dict | list):
            return None
        token = text_of(cursor)
        if token in self.cursors:  # the server repeats itself: nothing new follows
            return None
        self.cursors.add(token)
        return self.base.copy_set_param(rule.cursor_param, token)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class HttpJsonConnector:
    source: Source
    settings: HttpJsonSourceSettings
    allowed_secrets: tuple[str, ...] = ()
    transport: httpx.AsyncBaseTransport | None = None
    resolve: Resolver = resolve_host
    sleep: Sleep = asyncio.sleep
    clock: Callable[[], datetime] = utc_now

    @property
    def columns(self) -> tuple[str, ...]:
        return ()

    def _headers(self, options: HttpOptions) -> dict[str, str]:
        headers = {"Accept": ACCEPT}
        for name, value in options.headers.items():
            if value.startswith(SECRET_REFERENCE_PREFIX):
                if value not in self.allowed_secrets:
                    raise SourceDefinitionError(
                        f"header {name!r}: {value!r} is not in scribe.sources.allowed_connections"
                    )
                try:
                    value = resolve_secret(value)
                except SecretResolutionError as error:
                    raise SourceDefinitionError(f"header {name!r}: {error}") from None
            elif credential_header(name):
                raise SourceDefinitionError(
                    f"header {name!r} carries credentials: its value must be a secret reference"
                )
            headers[name] = value
        return headers

    def _first_url(self, options: HttpOptions, since: Any) -> httpx.URL:
        try:
            url = httpx.URL(resolve_location(self.source))
        except httpx.InvalidURL:
            raise SourceDefinitionError("connection: not a valid URL") from None
        if since is not None and options.since_param is not None:
            url = url.copy_set_param(options.since_param, text_of(since))
        return url

    def _delay(self, attempt: int, fetched: Fetched | None) -> float:
        settings = self.settings
        delay = settings.backoff_base_s * settings.backoff_factor ** (attempt - 1)
        if fetched is not None:
            asked = retry_after(fetched.headers, self.clock())
            if asked is not None:
                delay = max(delay, asked)
        return min(delay, settings.backoff_max_s)

    async def _get(
        self, client: PinnedClient, url: httpx.URL, headers: Mapping[str, str], what: str
    ) -> Fetched:
        """A 200 response, after retries of transient failures."""
        settings = self.settings
        attempt = 0
        while True:
            attempt += 1
            fetched: Fetched | None = None
            try:
                async with asyncio.timeout(settings.timeout_s):
                    fetched = await client.get(url, headers, what)
            except TimeoutError:
                failure = f"not complete within {SECTION}.timeout_s"
            except httpx.TransportError as error:
                failure = type(error).__name__
            except httpx.HTTPError as error:
                raise SourceReadError(f"GET {url.host}: {type(error).__name__}") from None
            else:
                if fetched.status == 200:
                    return fetched
                if fetched.status not in settings.retry_statuses:
                    raise SourceReadError(f"GET {url.host}: HTTP {fetched.status}")
                failure = f"HTTP {fetched.status}"
            if attempt >= settings.max_attempts:
                raise SourceReadError(f"GET {url.host}: {failure} after {attempt} attempts")
            await self.sleep(self._delay(attempt, fetched))

    async def rows(self, since: Any) -> AsyncIterator[tuple[str, Row]]:
        options = self.source.http or HttpOptions()
        settings = self.settings
        headers = self._headers(options)
        first = self._first_url(options, since)
        name = PurePosixPath(first.path).name or first.host
        pages = _Pages(options.pagination, first)
        limit = min(options.max_pages or settings.max_pages, settings.max_pages)
        rules = HttpRules(
            SECTION,
            settings.allowed_schemes,
            settings.allowed_hosts,
            settings.allow_private_targets,
            settings.max_bytes,
        )
        separator = self.source.flatten_separator
        async with httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=settings.timeout_s,
            transport=self.transport,
        ) as http:
            client = PinnedClient(http, rules, self.resolve)
            url: httpx.URL | None = pages.first()
            page = 0
            number = 0
            while url is not None:
                page += 1
                if page > limit:
                    raise SourceDefinitionError(
                        f"{name}: more than {limit} pages; raise {SECTION}.max_pages or the "
                        "Source's http.max_pages, or narrow the Source"
                    )
                fetched = await self._get(
                    client, url, headers, "connection" if page == 1 else f"page {page}"
                )
                try:
                    document = json.loads(fetched.body)
                except ValueError:
                    raise SourceReadError(f"{name}: page {page} is not valid JSON") from None
                records = records_at(document, self.source.records_path, f"{name} page {page}")
                for record in records:
                    number += 1
                    row = flatten(record, separator)
                    if keep(row, self.source, since):
                        yield f"{name}:{number}", row
                url = pages.next(url, fetched, document, len(records))
