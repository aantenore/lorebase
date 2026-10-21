"""The http_json connector against mocked transports: no network, no real lookups."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from lorebase_core.model import Schema, Source
from lorebase_core.model.source import (
    HttpOptions,
    HttpPagination,
    PaginationType,
    SourceType,
    SyncMode,
)
from lorebase_scribe.settings import HttpJsonSourceSettings, SourceSettings
from lorebase_scribe.sources.access import SourceReadError
from lorebase_scribe.sources.connectors import default_connector
from lorebase_scribe.sources.http_json import HttpJsonConnector, next_link, retry_after
from lorebase_scribe.sources.mapping import SourceDefinitionError

HOST = "api.example.test"
URL = f"https://{HOST}/v1/suppliers"
PUBLIC = "93.184.215.14"
TOKEN = "secret://env/TEST_API_TOKEN"
SETTINGS = HttpJsonSourceSettings(allowed_hosts=(HOST,), backoff_base_s=0.5)

Handler = Callable[[httpx.Request], httpx.Response]


def http_source(
    schema: Schema, monkeypatch: pytest.MonkeyPatch, url: str = URL, **changes: Any
) -> Source:
    monkeypatch.setenv("TEST_API_URL", url)
    monkeypatch.setenv("TEST_API_TOKEN", "Bearer t0ken")
    found = schema.source_by_api_name("approved.suppliers")
    assert found is not None
    return found.model_copy(
        update={
            "type": SourceType.HTTP_JSON,
            "connection": "secret://env/TEST_API_URL",
            **changes,
        }
    )


def resolver(*addresses: str) -> Any:
    async def resolve(host: str) -> list[str]:
        return list(addresses)

    return resolve


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def connector(
    source: Source,
    handler: Handler,
    settings: HttpJsonSourceSettings = SETTINGS,
    *,
    addresses: tuple[str, ...] = (PUBLIC,),
    sleeps: Sleeps | None = None,
) -> HttpJsonConnector:
    return HttpJsonConnector(
        source,
        settings,
        allowed_secrets=(TOKEN,),
        transport=httpx.MockTransport(handler),
        resolve=resolver(*addresses),
        sleep=sleeps or Sleeps(),
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )


async def collect(reader: Any, since: Any = None) -> list[tuple[str, dict[str, Any]]]:
    return [(locator, dict(row)) async for locator, row in reader.rows(since)]


def page(records: list[dict[str, Any]], **extra: Any) -> bytes:
    return json.dumps({"data": records, **extra}).encode()


def recorder(responses: list[httpx.Response], seen: list[httpx.Request]) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.pop(0)

    return handle


# One page, pinning, headers ---------------------------------------------------------------------


async def test_one_page_pinned_to_the_checked_address(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = HttpOptions(headers={"Authorization": TOKEN, "X-Tenant": "acme"})
    source = http_source(
        supply_schema, monkeypatch, records_path="data", flatten_separator=".", http=options
    )
    seen: list[httpx.Request] = []
    body = page([{"supplier_code": "S-1", "address": {"city": "Turin"}}, {"supplier_code": "S-2"}])
    reader = connector(source, recorder([httpx.Response(200, content=body)], seen))
    rows = await collect(reader)
    assert rows == [
        ("suppliers:1", {"supplier_code": "S-1", "address.city": "Turin"}),
        ("suppliers:2", {"supplier_code": "S-2"}),
    ]
    assert [str(request.url) for request in seen] == [f"https://{PUBLIC}/v1/suppliers"]
    request = seen[0]
    assert request.headers["Host"] == HOST
    assert request.extensions["sni_hostname"] == HOST
    assert request.headers["Authorization"] == "Bearer t0ken"
    assert request.headers["X-Tenant"] == "acme"
    assert request.headers["Accept"] == "application/json"


async def test_a_bare_array_is_the_records(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = http_source(supply_schema, monkeypatch)
    body = json.dumps([{"supplier_code": "S-1"}]).encode()
    rows = await collect(connector(source, lambda request: httpx.Response(200, content=body)))
    assert rows == [("suppliers:1", {"supplier_code": "S-1"})]


async def test_header_secrets_must_be_allowed_and_credentials_never_literal(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = lambda request: httpx.Response(200, content=b"[]")  # noqa: E731
    other = HttpOptions(headers={"Authorization": "secret://env/LOREBASE_DATABASE_DSN"})
    source = http_source(supply_schema, monkeypatch, http=other)
    with pytest.raises(SourceDefinitionError, match="allowed_connections") as caught:
        await collect(connector(source, ok))
    assert "t0ken" not in str(caught.value)
    # The model refuses literal credentials; a copy that skipped validation is refused too.
    with pytest.raises(ValueError, match="secret reference"):
        HttpOptions(headers={"X-Api-Key": "plain"})
    literal = HttpOptions().model_copy(update={"headers": {"X-Api-Key": "plain"}})
    source = http_source(supply_schema, monkeypatch, http=literal)
    with pytest.raises(SourceDefinitionError, match="credentials"):
        await collect(connector(source, ok))


def test_the_factory_passes_the_allowed_secrets(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = http_source(supply_schema, monkeypatch)
    settings = SourceSettings(allowed_connections=("secret://env/TEST_API_URL", TOKEN))
    built = default_connector(source, settings)
    assert isinstance(built, HttpJsonConnector)
    assert built.allowed_secrets == settings.allowed_connections


# Pagination -------------------------------------------------------------------------------------


async def test_cursor_pagination(supply_schema: Schema, monkeypatch: pytest.MonkeyPatch) -> None:
    pagination = HttpPagination(
        type=PaginationType.CURSOR, cursor_path="meta.next", cursor_param="after"
    )
    source = http_source(
        supply_schema, monkeypatch, records_path="data", http=HttpOptions(pagination=pagination)
    )
    seen: list[httpx.Request] = []
    responses = [
        httpx.Response(200, content=page([{"supplier_code": "S-1"}], meta={"next": "c2"})),
        httpx.Response(200, content=page([{"supplier_code": "S-2"}], meta={"next": "c3"})),
        httpx.Response(200, content=page([{"supplier_code": "S-3"}], meta={"next": None})),
    ]
    rows = await collect(connector(source, recorder(responses, seen)))
    assert [row["supplier_code"] for _, row in rows] == ["S-1", "S-2", "S-3"]
    assert [locator for locator, _ in rows] == ["suppliers:1", "suppliers:2", "suppliers:3"]
    assert [request.url.params.get("after") for request in seen] == [None, "c2", "c3"]


async def test_a_repeated_cursor_ends_the_read(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    pagination = HttpPagination(type=PaginationType.CURSOR, cursor_path="next")
    source = http_source(
        supply_schema, monkeypatch, records_path="data", http=HttpOptions(pagination=pagination)
    )
    body = page([{"supplier_code": "S-1"}], next="same")
    seen: list[httpx.Request] = []
    responses = [httpx.Response(200, content=body) for _ in range(3)]
    await collect(connector(source, recorder(responses, seen)))
    assert len(seen) == 2


async def test_page_pagination_stops_at_an_empty_page(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    pagination = HttpPagination(
        type=PaginationType.PAGE, page_param="p", start_page=1, size_param="per_page", page_size=2
    )
    source = http_source(
        supply_schema, monkeypatch, records_path="data", http=HttpOptions(pagination=pagination)
    )
    seen: list[httpx.Request] = []
    responses = [
        httpx.Response(200, content=page([{"supplier_code": "S-1"}, {"supplier_code": "S-2"}])),
        httpx.Response(200, content=page([{"supplier_code": "S-3"}])),
        httpx.Response(200, content=page([])),
    ]
    rows = await collect(connector(source, recorder(responses, seen)))
    assert len(rows) == 3
    assert [(r.url.params["p"], r.url.params["per_page"]) for r in seen] == [
        ("1", "2"),
        ("2", "2"),
        ("3", "2"),
    ]


async def test_offset_pagination_moves_by_the_records_received(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    pagination = HttpPagination(type=PaginationType.OFFSET, size_param="limit", page_size=100)
    source = http_source(
        supply_schema, monkeypatch, records_path="data", http=HttpOptions(pagination=pagination)
    )
    seen: list[httpx.Request] = []
    responses = [
        httpx.Response(200, content=page([{"supplier_code": f"S-{n}"} for n in range(3)])),
        httpx.Response(200, content=page([{"supplier_code": "S-3"}])),
        httpx.Response(200, content=page([])),
    ]
    rows = await collect(connector(source, recorder(responses, seen)))
    assert len(rows) == 4
    assert [request.url.params["offset"] for request in seen] == ["0", "3", "4"]


async def test_link_header_pagination(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    pagination = HttpPagination(type=PaginationType.LINK)
    source = http_source(supply_schema, monkeypatch, http=HttpOptions(pagination=pagination))
    seen: list[httpx.Request] = []
    responses = [
        httpx.Response(
            200,
            content=b'[{"supplier_code": "S-1"}]',
            headers={"Link": '</v1/suppliers?page=2>; rel="next", </v1/x?page=9>; rel="last"'},
        ),
        httpx.Response(200, content=b'[{"supplier_code": "S-2"}]'),
    ]
    rows = await collect(connector(source, recorder(responses, seen)))
    assert len(rows) == 2
    assert str(seen[1].url) == f"https://{PUBLIC}/v1/suppliers?page=2"
    assert seen[1].headers["Host"] == HOST


async def test_a_next_link_to_another_host_is_refused(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    pagination = HttpPagination(type=PaginationType.LINK)
    source = http_source(supply_schema, monkeypatch, http=HttpOptions(pagination=pagination))
    seen: list[httpx.Request] = []
    responses = [
        httpx.Response(
            200,
            content=b'[{"supplier_code": "S-1"}]',
            headers={"Link": '<https://evil.test/steal>; rel="next"'},
        )
    ]
    with pytest.raises(SourceDefinitionError, match=r"page 2: the URL host is not in"):
        await collect(connector(source, recorder(responses, seen)))
    assert len(seen) == 1


async def test_more_pages_than_allowed_fail_the_read(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    pagination = HttpPagination(type=PaginationType.PAGE)
    options = HttpOptions(pagination=pagination, max_pages=5)
    source = http_source(supply_schema, monkeypatch, http=options)
    endless = lambda request: httpx.Response(200, content=b'[{"supplier_code": "S-1"}]')  # noqa: E731
    capped = HttpJsonSourceSettings(allowed_hosts=(HOST,), max_pages=3)
    with pytest.raises(SourceDefinitionError, match="more than 3 pages"):
        await collect(connector(source, endless, capped))
    with pytest.raises(SourceDefinitionError, match="more than 5 pages"):
        await collect(connector(source, endless))


def test_next_link_parsing() -> None:
    assert next_link('<https://a.test/2>; rel="next"') == "https://a.test/2"
    assert next_link('<https://a.test/1>; rel="prev", <https://a.test/3>; rel=next') == (
        "https://a.test/3"
    )
    assert next_link('<https://a.test/3>; rel="last next"') == "https://a.test/3"
    assert next_link('<https://a.test/1>; rel="prev"') is None
    assert next_link(None) is None


# Retries ----------------------------------------------------------------------------------------


async def test_429_and_5xx_are_retried_with_backoff(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = http_source(supply_schema, monkeypatch)
    seen: list[httpx.Request] = []
    responses = [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(503),
        httpx.Response(200, content=b'[{"supplier_code": "S-1"}]'),
    ]
    sleeps = Sleeps()
    rows = await collect(connector(source, recorder(responses, seen), sleeps=sleeps))
    assert len(rows) == 1
    assert len(seen) == 3
    assert sleeps.delays == [7.0, 1.0]  # Retry-After, then base 0.5 * factor 2


async def test_retries_stop_after_max_attempts(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = http_source(supply_schema, monkeypatch)
    seen: list[httpx.Request] = []
    settings = HttpJsonSourceSettings(allowed_hosts=(HOST,), max_attempts=3, backoff_max_s=0.75)
    sleeps = Sleeps()
    responses = [httpx.Response(500) for _ in range(5)]
    with pytest.raises(SourceReadError, match="HTTP 500 after 3 attempts"):
        await collect(connector(source, recorder(responses, seen), settings, sleeps=sleeps))
    assert len(seen) == 3
    assert sleeps.delays == [0.75, 0.75]  # capped by backoff_max_s


@pytest.mark.parametrize("status", [302, 401, 404])
async def test_other_statuses_fail_at_once(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    source = http_source(supply_schema, monkeypatch)
    seen: list[httpx.Request] = []
    headers = {"location": "http://169.254.169.254/"} if status == 302 else {}
    responses = [httpx.Response(status, headers=headers)]
    with pytest.raises(SourceReadError, match=f"HTTP {status}"):
        await collect(connector(source, recorder(responses, seen)))
    assert len(seen) == 1  # no retry, no redirect followed


async def test_a_slow_server_times_out_and_is_retried(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, content=b"[]")

    source = http_source(supply_schema, monkeypatch)
    settings = HttpJsonSourceSettings(allowed_hosts=(HOST,), timeout_s=0.01, max_attempts=2)
    reader = HttpJsonConnector(
        source,
        settings,
        transport=httpx.MockTransport(slow),
        resolve=resolver(PUBLIC),
        sleep=Sleeps(),
    )
    with pytest.raises(SourceReadError, match=r"timeout_s after 2 attempts"):
        await collect(reader)


def test_retry_after_forms() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert retry_after(httpx.Headers({"Retry-After": "12"}), now) == 12.0
    date = httpx.Headers({"Retry-After": "Thu, 01 Jan 2026 00:00:30 GMT"})
    assert retry_after(date, now) == 30.0
    assert retry_after(httpx.Headers({"Retry-After": "soon"}), now) is None
    assert retry_after(httpx.Headers(), now) is None


# Address checks and limits ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "addresses",
    [("10.0.0.5",), ("127.0.0.1",), (PUBLIC, "169.254.169.254"), ("100.64.0.1",), ("::1",)],
)
async def test_hosts_resolving_to_private_addresses_are_refused(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch, addresses: tuple[str, ...]
) -> None:
    source = http_source(supply_schema, monkeypatch)
    seen: list[httpx.Request] = []
    reader = connector(source, recorder([], seen), addresses=addresses)
    with pytest.raises(SourceDefinitionError, match="allow_private_targets") as caught:
        await collect(reader)
    assert seen == []
    assert HOST not in str(caught.value)
    assert not any(address in str(caught.value) for address in addresses)


@pytest.mark.parametrize(
    "url", ["https://localhost/x", "https://127.0.0.1/x", "https://[::1]/x", "https://10.1.2.3/x"]
)
async def test_private_literal_hosts_are_refused_even_when_listed(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    host = httpx.URL(url).host
    source = http_source(supply_schema, monkeypatch, url=url)
    settings = HttpJsonSourceSettings(allowed_hosts=(host,))
    seen: list[httpx.Request] = []
    with pytest.raises(SourceDefinitionError, match="allow_private_targets"):
        await collect(connector(source, recorder([], seen), settings, addresses=("127.0.0.1",)))
    assert seen == []


async def test_private_hosts_can_be_allowed(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = http_source(supply_schema, monkeypatch, url="https://api.internal.test/v1/s")
    settings = HttpJsonSourceSettings(
        allowed_hosts=("api.internal.test",), allow_private_targets=True
    )
    seen: list[httpx.Request] = []
    responses = [httpx.Response(200, content=b"[]")]
    await collect(connector(source, recorder(responses, seen), settings, addresses=("10.0.0.5",)))
    assert [str(request.url) for request in seen] == ["https://api.internal.test/v1/s"]


async def test_scheme_and_host_allowlists(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []
    plain = http_source(supply_schema, monkeypatch, url=f"http://{HOST}/v1/suppliers")
    with pytest.raises(SourceDefinitionError, match="allowed_schemes"):
        await collect(connector(plain, recorder([], seen)))
    other = http_source(supply_schema, monkeypatch, url="https://other.test/v1/suppliers")
    with pytest.raises(SourceDefinitionError, match=r"http_json\.allowed_hosts"):
        await collect(connector(other, recorder([], seen)))
    assert seen == []


async def test_responses_larger_than_max_bytes_are_refused(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = http_source(supply_schema, monkeypatch)
    settings = HttpJsonSourceSettings(allowed_hosts=(HOST,), max_bytes=10)
    big = lambda request: httpx.Response(200, content=b"[" + b'{"a": 1},' * 10 + b"{}]")  # noqa: E731
    with pytest.raises(SourceDefinitionError, match=r"http_json\.max_bytes"):
        await collect(connector(source, big, settings))


async def test_invalid_json_fails_the_read(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = http_source(supply_schema, monkeypatch)
    with pytest.raises(SourceReadError, match="page 1 is not valid JSON"):
        await collect(connector(source, lambda request: httpx.Response(200, content=b"<html>")))


# Incremental reads ------------------------------------------------------------------------------


async def test_incremental_reads_send_and_apply_the_watermark(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = http_source(
        supply_schema,
        monkeypatch,
        sync_mode=SyncMode.INCREMENTAL,
        watermark_column="updated_at",
        http=HttpOptions(since_param="updated_since"),
    )
    seen: list[httpx.Request] = []
    body = json.dumps(
        [
            {"supplier_code": "S-1", "updated_at": "2026-01-01T00:00:00Z"},
            {"supplier_code": "S-2", "updated_at": "2026-03-01T00:00:00Z"},
        ]
    ).encode()
    rows = await collect(
        connector(source, recorder([httpx.Response(200, content=body)], seen)),
        "2026-02-01T00:00:00Z",
    )
    assert [row["supplier_code"] for _, row in rows] == ["S-2"]
    assert seen[0].url.params["updated_since"] == "2026-02-01T00:00:00Z"
