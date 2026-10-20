"""Request layer of the sync and async clients, against mocked HTTP.

respx is used because it mocks `httpx` at the transport level for both the sync and the async
client with the same route definitions, and it records the requests so that tests assert on
headers, paths and bodies. Retry waits are captured through the `sleep` argument, so nothing
sleeps.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from lorebase_client import (
    AsyncLorebaseClient,
    LorebaseClient,
    LorebaseConnectionError,
    NotFoundError,
    RateLimitedError,
    RetryPolicy,
    StaleHeadError,
    UnavailableError,
    new_idempotency_key,
)

BASE = "http://keeper.test"
API = f"{BASE}/api/v1"
PROBLEM = "application/problem+json"
NO_JITTER = RetryPolicy(max_retries=3, backoff_base_s=1.0, backoff_max_s=10.0, jitter=0.0)

DOMAIN = {
    "id": "supply",
    "display_name": "Supply chain",
    "description": "",
    "default_branch": "main",
    "settings": {},
    "created_at": "2026-10-01T08:00:00Z",
    "created_by": "usr_1",
    "head": None,
}


def problem(
    status: int, code: str, *, retry_after: int | None = None, **members: Any
) -> httpx.Response:
    body = {"type": f"t/{code}", "title": code, "status": status, "code": code, **members}
    headers = {"content-type": PROBLEM}
    if retry_after is not None:
        headers["retry-after"] = str(retry_after)
    return httpx.Response(status, json=body, headers=headers)


@pytest.fixture
def http() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


class Sleeps:
    """Records the waits between attempts."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)

    async def asleep(self, seconds: float) -> None:
        self.waits.append(seconds)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


def make(sleeps: Sleeps, **options: Any) -> LorebaseClient:
    options.setdefault("retry", NO_JITTER)
    return LorebaseClient(BASE, api_key="lbk_test", sleep=sleeps, env={}, **options)


def amake(sleeps: Sleeps, **options: Any) -> AsyncLorebaseClient:
    options.setdefault("retry", NO_JITTER)
    return AsyncLorebaseClient(BASE, api_key="lbk_test", sleep=sleeps.asleep, env={}, **options)


# Requests -----------------------------------------------------------------------------------


def test_request_carries_credentials_and_decodes_the_model(
    http: respx.MockRouter, sleeps: Sleeps
) -> None:
    route = http.get("/domains/supply").respond(200, json=DOMAIN)
    with make(sleeps) as client:
        domain = client.domains.get("supply")
    assert domain.id == "supply"
    assert domain.head is None
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer lbk_test"
    assert sent.headers["accept"] == "application/json"


def test_anonymous_client_sends_no_credentials(http: respx.MockRouter, sleeps: Sleeps) -> None:
    route = http.get("/domains/supply").respond(200, json=DOMAIN)
    with LorebaseClient(BASE, sleep=sleeps, env={}) as client:
        client.domains.get("supply")
    assert "authorization" not in route.calls.last.request.headers


def test_base_url_and_credentials_come_from_the_environment(
    http: respx.MockRouter,
) -> None:
    route = http.get("/auth/whoami").respond(
        200,
        json={
            "id": "u",
            "type": "user",
            "display_name": "U",
            "method": "oidc",
            "subjects": ["u"],
            "key_scopes": [],
            "installation_admin": False,
        },
    )
    env = {"LOREBASE_URL": BASE, "LOREBASE_TOKEN": "jwt-token"}
    with LorebaseClient(env=env) as client:
        assert client.whoami().method == "oidc"
    assert route.calls.last.request.headers["authorization"] == "Bearer jwt-token"


def test_query_parameters_paths_and_json_bodies(http: respx.MockRouter, sleeps: Sleeps) -> None:
    records = http.get("/domains/supply/kinds/Part/records").respond(
        200,
        json={"revision": "ab", "served_from": "materialized", "items": [], "next_cursor": None},
    )
    link = http.get("/domains/supply/kinds/Part/records/P%2F1/links/supplied_by").respond(
        200, json={"revision": "ab", "served_from": "tree", "items": []}
    )
    with make(sleeps) as client:
        client.records.list(
            "supply",
            "Part",
            branch="agent/bot-1/task",
            where={"attr": "category", "op": "eq", "value": "bearing"},
            order_by=["category", "key:desc"],
            select=["key", "title"],
            limit=5,
        )
        client.records.links("supply", "Part", "P/1", "supplied_by", at="v1")
    params = dict(records.calls.last.request.url.params)
    assert params == {
        "branch": "agent/bot-1/task",
        "where": '{"attr":"category","op":"eq","value":"bearing"}',
        "order_by": "category,key:desc",
        "select": "key,title",
        "limit": "5",
    }
    assert dict(link.calls.last.request.url.params) == {"at": "v1"}


def test_hierarchical_branch_names_stay_in_the_path(http: respx.MockRouter, sleeps: Sleeps) -> None:
    route = http.get("/domains/supply/branches/agent/bot-1/task").respond(
        200,
        json={
            "name": "agent/bot-1/task",
            "head": "aa",
            "protected": False,
            "default": False,
            "materialization": "full",
            "version": 1,
            "created_at": "2026-10-01T08:00:00Z",
            "created_by": "u",
            "ahead": 2,
            "behind": 0,
        },
    )
    with make(sleeps) as client:
        detail = client.branches.get("supply", "agent/bot-1/task")
    assert (detail.ahead, detail.behind) == (2, 0)
    assert route.called


def test_booleans_and_repeated_parameters(http: respx.MockRouter, sleeps: Sleeps) -> None:
    route = http.get("/domains/supply/diff").respond(
        200,
        json={"from_revision": "a", "to_revision": "b", "total": 0, "summary": [], "items": []},
    )
    with make(sleeps) as client:
        client.revisions.diff("supply", "main", "work", kinds=["Part", "Supplier"], schema=False)
    query = route.calls.last.request.url.params
    assert query.get_list("kinds") == ["Part", "Supplier"]
    assert query["schema"] == "false"
    assert query["from"] == "main"


def test_request_models_are_validated_before_sending(
    http: respx.MockRouter, sleeps: Sleeps
) -> None:
    from pydantic import ValidationError

    with make(sleeps) as client, pytest.raises(ValidationError):
        client.domains.create("Not A Slug")
    assert http.calls.call_count == 0


def test_write_body_contains_only_the_set_members(http: respx.MockRouter, sleeps: Sleeps) -> None:
    route = http.post("/domains/supply/branches").respond(
        201,
        json={
            "name": "work",
            "head": "aa",
            "protected": False,
            "default": False,
            "materialization": "full",
            "version": 1,
            "created_at": "2026-10-01T08:00:00Z",
            "created_by": "u",
        },
    )
    with make(sleeps) as client:
        client.branches.create("supply", "work", source="main")
    assert json.loads(route.calls.last.request.content) == {"name": "work", "source": "main"}


# Errors -------------------------------------------------------------------------------------


def test_problem_details_raise_typed_exceptions(http: respx.MockRouter, sleeps: Sleeps) -> None:
    http.get("/domains/none").mock(return_value=problem(404, "not-found", detail="No Domain."))
    http.post("/domains/supply/operations/import_records/execute").mock(
        return_value=problem(
            409, "stale-head", branch="work", expected_head="aa", current_head="bb"
        )
    )
    with make(sleeps) as client:
        with pytest.raises(NotFoundError) as missing:
            client.domains.get("none")
        assert missing.value.status == 404
        assert str(missing.value) == "No Domain."
        with pytest.raises(StaleHeadError) as stale:
            client.operations.import_records("supply", "Part", [{"part_number": "P-1"}])
        assert stale.value.current_head == "bb"


# Pagination ---------------------------------------------------------------------------------


def page(names: list[str], next_cursor: str | None) -> dict[str, Any]:
    return {
        "items": [{**DOMAIN, "id": name} for name in names],
        "next_cursor": next_cursor,
    }


def test_iterate_follows_cursors_lazily(http: respx.MockRouter, sleeps: Sleeps) -> None:
    pages = {
        None: page(["a", "b"], "c1"),
        "c1": page(["c", "d"], "c2"),
        "c2": page(["e"], None),
    }

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.params["limit"] == "2"
        return httpx.Response(200, json=pages[request.url.params.get("cursor")])

    route = http.get("/domains").mock(side_effect=answer)
    with make(sleeps) as client:
        iterator = client.domains.iterate(page_size=2)
        assert route.call_count == 0  # nothing is fetched before the first item is needed
        first = next(iterator)
        assert first.id == "a"
        assert route.call_count == 1
        rest = [d.id for d in iterator]
    assert rest == ["b", "c", "d", "e"]
    assert route.call_count == 3


def test_list_returns_one_page_with_its_cursor(http: respx.MockRouter, sleeps: Sleeps) -> None:
    http.get("/domains").respond(200, json=page(["a"], "next"))
    with make(sleeps) as client:
        result = client.domains.list(limit=1)
    assert [d.id for d in result.items] == ["a"]
    assert result.next_cursor == "next"


def test_query_iteration_posts_the_cursor_in_the_body(
    http: respx.MockRouter, sleeps: Sleeps
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        cursor = body.get("cursor")
        item = {"kind": "Part", "key": "P-2" if cursor else "P-1", "values": {}}
        return httpx.Response(
            200,
            json={
                "revision": "ab",
                "served_from": "materialized",
                "items": [item],
                "next_cursor": None if cursor else "c1",
            },
        )

    route = http.post("/domains/supply/query").mock(side_effect=answer)
    with make(sleeps) as client:
        keys = [r.key for r in client.records.iterate_query("supply", {"kind": "Part", "limit": 1})]
    assert keys == ["P-1", "P-2"]
    assert json.loads(route.calls[0].request.content) == {"kind": "Part", "limit": 1}
    assert json.loads(route.calls[1].request.content) == {
        "kind": "Part",
        "limit": 1,
        "cursor": "c1",
    }


# Idempotency --------------------------------------------------------------------------------


def test_idempotency_key_helper_and_header(http: respx.MockRouter, sleeps: Sleeps) -> None:
    route = http.post("/domains").respond(201, json=DOMAIN)
    key = new_idempotency_key("import")
    assert key.startswith("import-")
    assert len(key) <= 255
    assert new_idempotency_key() != new_idempotency_key()
    with make(sleeps) as client:
        client.domains.create("supply", idempotency_key=key)
        client.domains.create("supply")
    assert route.calls[0].request.headers["idempotency-key"] == key
    assert "idempotency-key" not in route.calls[1].request.headers


def test_auto_idempotency_keys_cover_creates_and_executions(
    http: respx.MockRouter, sleeps: Sleeps
) -> None:
    create = http.post("/domains").respond(201, json=DOMAIN)
    execute = http.post("/domains/supply/query").respond(
        200, json={"revision": "a", "served_from": "tree", "items": []}
    )
    with make(sleeps, auto_idempotency_keys=True) as client:
        client.domains.create("supply")
        client.records.query("supply", {"kind": "Part"})
    assert create.calls.last.request.headers["idempotency-key"]
    assert "idempotency-key" not in execute.calls.last.request.headers


# Retries ------------------------------------------------------------------------------------


def test_rate_limit_and_busy_branch_are_retried_honoring_retry_after(
    http: respx.MockRouter, sleeps: Sleeps
) -> None:
    route = http.post("/domains/supply/operations/import_records/execute").mock(
        side_effect=[
            problem(429, "rate-limited", retry_after=4),
            problem(503, "branch-busy", retry_after=2),
            httpx.Response(200, json=_execution()),
        ]
    )
    with make(sleeps) as client:
        result = client.operations.import_records("supply", "Part", [])
    assert result.status == "committed"
    assert route.call_count == 3
    assert sleeps.waits == [4.0, 2.0]


def test_backoff_doubles_up_to_the_cap(http: respx.MockRouter, sleeps: Sleeps) -> None:
    route = http.get("/domains/supply").mock(
        side_effect=[problem(503, "unavailable")] * 3 + [httpx.Response(200, json=DOMAIN)]
    )
    policy = RetryPolicy(max_retries=3, backoff_base_s=1.0, backoff_max_s=3.0, jitter=0.0)
    with make(sleeps, retry=policy) as client:
        client.domains.get("supply")
    assert route.call_count == 4
    assert sleeps.waits == [1.0, 2.0, 3.0]


def test_retries_stop_after_the_limit_and_raise_the_last_error(
    http: respx.MockRouter, sleeps: Sleeps
) -> None:
    route = http.get("/domains/supply").mock(return_value=problem(429, "rate-limited"))
    policy = RetryPolicy(max_retries=2, backoff_base_s=0.0, jitter=0.0)
    with make(sleeps, retry=policy) as client, pytest.raises(RateLimitedError):
        client.domains.get("supply")
    assert route.call_count == 3
    assert len(sleeps.waits) == 2


def test_other_client_errors_are_not_retried(http: respx.MockRouter, sleeps: Sleeps) -> None:
    route = http.get("/domains/supply").mock(return_value=problem(404, "not-found"))
    with make(sleeps) as client, pytest.raises(NotFoundError):
        client.domains.get("supply")
    assert route.call_count == 1
    assert sleeps.waits == []


def test_proxy_503_is_retried_for_reads_but_not_for_plain_writes(
    http: respx.MockRouter, sleeps: Sleeps
) -> None:
    read = http.get("/domains/supply").mock(
        side_effect=[httpx.Response(503, text="upstream down"), httpx.Response(200, json=DOMAIN)]
    )
    write = http.post("/domains").mock(return_value=httpx.Response(503, text="upstream down"))
    with make(sleeps) as client:
        client.domains.get("supply")
        with pytest.raises(UnavailableError):
            client.domains.create("supply")
        with pytest.raises(UnavailableError):
            client.domains.create("supply", idempotency_key="k1")
    assert read.call_count == 2
    assert write.call_count == 1 + 1 + 3  # one try, then a key makes retries safe


def test_connection_failures_are_retried_for_every_request(
    http: respx.MockRouter, sleeps: Sleeps
) -> None:
    route = http.post("/domains").mock(
        side_effect=[httpx.ConnectError("refused"), httpx.Response(201, json=DOMAIN)]
    )
    with make(sleeps) as client:
        assert client.domains.create("supply").id == "supply"
    assert route.call_count == 2


def test_read_timeouts_are_retried_only_when_the_request_is_repeatable(
    http: respx.MockRouter, sleeps: Sleeps
) -> None:
    http.get("/domains/supply").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json=DOMAIN)]
    )
    write = http.post("/domains").mock(side_effect=httpx.ReadTimeout("slow"))
    with make(sleeps) as client:
        client.domains.get("supply")
        with pytest.raises(LorebaseConnectionError):
            client.domains.create("supply")
    assert write.call_count == 1


def test_exhausted_connection_retries_raise_a_connection_error(
    http: respx.MockRouter, sleeps: Sleeps
) -> None:
    route = http.get("/domains/supply").mock(side_effect=httpx.ConnectError("refused"))
    policy = RetryPolicy(max_retries=1, backoff_base_s=0.0, jitter=0.0)
    with make(sleeps, retry=policy) as client, pytest.raises(LorebaseConnectionError) as raised:
        client.domains.get("supply")
    assert route.call_count == 2
    assert "refused" in str(raised.value)
    assert raised.value.status is None


def test_jitter_shortens_the_computed_wait(http: respx.MockRouter, sleeps: Sleeps) -> None:
    http.get("/domains/supply").mock(
        side_effect=[problem(429, "rate-limited"), httpx.Response(200, json=DOMAIN)]
    )
    policy = RetryPolicy(max_retries=1, backoff_base_s=4.0, backoff_max_s=10.0, jitter=0.5)
    with make(sleeps, retry=policy) as client:
        client.domains.get("supply")
    assert 2.0 <= sleeps.waits[0] <= 4.0


# ETag cache ---------------------------------------------------------------------------------

RECORD = {
    "revision": "ab",
    "served_from": "materialized",
    "record": {"kind": "Part", "key": "P-1", "values": {"name": "Seal"}},
}


def test_etag_cache_revalidates_and_serves_the_cached_body(
    http: respx.MockRouter, sleeps: Sleeps
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == '"ab.1"':
            return httpx.Response(304, headers={"etag": '"ab.1"'})
        return httpx.Response(200, json=RECORD, headers={"etag": '"ab.1"'})

    route = http.get("/domains/supply/kinds/Part/records/P-1").mock(side_effect=answer)
    with make(sleeps, cache=True) as client:
        first = client.records.get("supply", "Part", "P-1")
        second = client.records.get("supply", "Part", "P-1")
        other = client.records.get("supply", "Part", "P-1", links=True)
    assert first == second
    assert route.call_count == 3
    assert "if-none-match" not in route.calls[0].request.headers
    assert route.calls[1].request.headers["if-none-match"] == '"ab.1"'
    assert "if-none-match" not in route.calls[2].request.headers  # another URL, another entry
    assert other.record.key == "P-1"


def test_etag_cache_is_off_by_default_and_skips_writes(
    http: respx.MockRouter, sleeps: Sleeps
) -> None:
    route = http.get("/domains/supply/kinds/Part/records/P-1").respond(
        200, json=RECORD, headers={"etag": '"ab.1"'}
    )
    with make(sleeps) as client:
        client.records.get("supply", "Part", "P-1")
        client.records.get("supply", "Part", "P-1")
    assert all("if-none-match" not in call.request.headers for call in route.calls)


def test_etag_cache_evicts_the_least_recently_used_entry(
    http: respx.MockRouter, sleeps: Sleeps
) -> None:
    http.get(url__regex=r"/domains/supply/kinds/Part/records/P-\d").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={**RECORD, "record": {"kind": "Part", "key": request.url.path[-3:], "values": {}}},
            headers={"etag": f'"{request.url.path[-3:]}"'},
        )
    )
    with make(sleeps, cache=2) as client:
        for key in ("P-1", "P-2", "P-3"):
            client.records.get("supply", "Part", key)
        cache = client._core.cache
        assert cache is not None
        assert len(cache) == 2
        client.cache_clear()
        assert len(cache) == 0


# Async --------------------------------------------------------------------------------------


async def test_async_client_pages_retries_and_maps_errors(
    http: respx.MockRouter, sleeps: Sleeps
) -> None:
    route = http.get("/domains").mock(
        side_effect=[
            problem(429, "rate-limited"),
            httpx.Response(200, json=page(["a"], "c1")),
            httpx.Response(200, json=page(["b"], None)),
        ]
    )
    http.get("/domains/none").mock(return_value=problem(404, "not-found"))
    async with amake(sleeps) as client:
        ids = [d.id async for d in client.domains.iterate()]
        assert ids == ["a", "b"]
        with pytest.raises(NotFoundError):
            await client.domains.get("none")
    assert route.call_count == 3
    assert sleeps.waits == [1.0]


async def test_async_etag_cache_and_idempotency(http: respx.MockRouter, sleeps: Sleeps) -> None:
    get = http.get("/domains/supply/kinds/Part/records/P-1").mock(
        side_effect=[
            httpx.Response(200, json=RECORD, headers={"etag": '"x"'}),
            httpx.Response(304),
        ]
    )
    create = http.post("/domains").respond(201, json=DOMAIN)
    async with amake(sleeps, cache=True, auto_idempotency_keys=True) as client:
        a = await client.records.get("supply", "Part", "P-1")
        b = await client.records.get("supply", "Part", "P-1")
        await client.domains.create("supply")
    assert a == b
    assert get.calls[1].request.headers["if-none-match"] == '"x"'
    assert create.calls.last.request.headers["idempotency-key"]


async def test_async_connection_failure_is_typed(http: respx.MockRouter, sleeps: Sleeps) -> None:
    http.get("/auth/whoami").mock(side_effect=httpx.ConnectError("down"))
    policy = RetryPolicy(max_retries=1, backoff_base_s=0.0, jitter=0.0)
    async with amake(sleeps, retry=policy) as client:
        with pytest.raises(LorebaseConnectionError):
            await client.whoami()
    assert len(sleeps.waits) == 1


def _execution() -> dict[str, Any]:
    return {
        "status": "committed",
        "operation": "import_records",
        "branch": "work",
        "invocation_id": "inv_1",
        "parameters_hash": "ff",
        "head": "aa",
        "revision": "bb",
        "revisions": ["bb"],
        "edits": [],
        "edit_count": 0,
        "edits_truncated": False,
        "violations": [],
        "violation_count": 0,
        "warnings": [],
        "review_required": False,
        "replayed": False,
    }
