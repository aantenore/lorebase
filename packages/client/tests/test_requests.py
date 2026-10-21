"""Request layer of the sync and async clients, against mocked HTTP.

respx is used because it mocks `httpx` at the transport level for both the sync and the async
client with the same route definitions, and it records the requests so that tests assert on
headers, paths and bodies. Retry waits are captured through the `sleep` argument, so nothing
sleeps.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError

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
from lorebase_client import models as m

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


def test_import_batch_sends_records_and_links_in_one_call(
    http: respx.MockRouter, sleeps: Sleeps
) -> None:
    route = http.post("/domains/supply/operations/import_batch/execute").respond(
        200, json=_execution()
    )
    records = [{"kind": "Contract", "contract_number": "C-1"}]
    links = [{"relation": "with_supplier", "from": "C-1", "to": "S-1"}]
    with make(sleeps) as client:
        client.operations.import_batch(
            "supply", records, links, branch="work", message="Load", idempotency_key="k1"
        )
        client.operations.import_batch("supply", links=links)
    first, second = (json.loads(call.request.content) for call in route.calls)
    assert first == {
        "parameters": {"records": records, "links": links},
        "branch": "work",
        "dry_run": False,
        "message": "Load",
    }
    assert route.calls[0].request.headers["idempotency-key"] == "k1"
    assert second["parameters"] == {"records": [], "links": links}


async def test_async_import_batch(http: respx.MockRouter, sleeps: Sleeps) -> None:
    route = http.post("/domains/supply/operations/import_batch/execute").respond(
        200, json=_execution()
    )
    async with amake(sleeps) as client:
        result = await client.operations.import_batch("supply", [{"kind": "Plant"}], dry_run=True)
    assert result.status == "committed"
    body = json.loads(route.calls.last.request.content)
    assert body["parameters"] == {"records": [{"kind": "Plant"}], "links": []}
    assert body["dry_run"] is True


def test_execute_as_agent_returns_the_proposal_of_a_202(
    http: respx.MockRouter, sleeps: Sleeps
) -> None:
    proposed = {
        **_execution(),
        "status": "proposed",
        "target": "main",
        "branch": "agent/svc_1/audit",
        "proposal": {
            "id": "prp_1",
            "title": "Agent svc_1: audit",
            "status": "open",
            "source_ref": "agent/svc_1/audit",
            "target_ref": "main",
            "url": "/api/v1/domains/supply/proposals/prp_1",
            "created": True,
        },
        "approval": {"required": 1, "approvals": 0, "satisfied": False},
        "attempt": None,
        "merge": {"requested": False, "allowed": False, "performed": False, "blockers": []},
        "merged_revision": None,
        "branch_created": True,
        "branch_deleted": False,
        "next": [{"action": "get_proposal", "detail": "Read it."}],
    }
    route = http.post("/domains/supply/operations/flag_supplier_risk/execute")
    route.side_effect = [httpx.Response(202, json=proposed), httpx.Response(200, json=_execution())]
    with make(sleeps) as client:
        first = client.operations.execute_as_agent(
            "supply", "flag_supplier_risk", {"supplier": "S-1"}, task="audit", intent="Review"
        )
        second = client.operations.execute_as_agent("supply", "flag_supplier_risk", task="audit")
    assert isinstance(first, m.AgentWrite)
    assert (first.status, first.proposal.id, first.next[0].action) == (
        "proposed",
        "prp_1",
        "get_proposal",
    )
    assert isinstance(second, m.Execution)
    body = json.loads(route.calls[0].request.content)
    assert (body["task"], body["intent"]) == ("audit", "Review")
    assert "auto_merge" not in body


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


# Proposals, agents and webhooks -------------------------------------------------------------

FINGERPRINT = "ab" * 32


def _proposal(**members: Any) -> dict[str, Any]:
    return {
        "id": "prp_1",
        "title": "Add suppliers",
        "description": None,
        "source_ref": "work",
        "target_ref": "main",
        "origin": "manual",
        "status": "open",
        "author": "usr_1",
        "created_at": "2026-10-01T08:00:00Z",
        "updated_at": None,
        "merged_revision": None,
        "policy": {},
        "checks": [{"name": "schema", "status": "passed"}],
        **members,
    }


def _detail() -> dict[str, Any]:
    return {
        **_proposal(),
        "reviews": [],
        "approval": {
            "protected": False,
            "required": 0,
            "approvals": 0,
            "approvers": [],
            "changes_requested": [],
            "satisfied": True,
        },
        "attempt": None,
    }


def _attempt() -> dict[str, Any]:
    return {
        "id": "att_1",
        "proposal": "prp_1",
        "status": "conflicted",
        "result": None,
        "base": "aa",
        "ours": "bb",
        "theirs": "cc",
        "virtual_base": False,
        "created_at": "2026-10-01T08:00:00Z",
        "created_by": "usr_1",
        "revision": None,
        "policy": {},
        "open_conflicts": 1,
        "resolved_conflicts": 0,
        "counts": {"DV1": {"open": 1}},
        "recomputed": False,
        "stale": False,
        "resolutions": [],
        "unmatched_resolutions": [],
        "auto_resolutions": [],
    }


def _conflicts(next_cursor: str | None = None) -> dict[str, Any]:
    conflict = {"id": "cf_1", "status": "open", "code": "DV1", "key": "P-1"}
    return {**_attempt(), "conflicts": [conflict], "next_cursor": next_cursor}


def test_create_proposal_sends_the_body_and_idempotency_key(
    http: respx.MockRouter, sleeps: Sleeps
) -> None:
    route = http.post("/domains/supply/proposals").respond(201, json=_detail())
    with make(sleeps) as client:
        created = client.proposals.create(
            "supply", "work", "main", title="Add suppliers", idempotency_key="k-1"
        )
    assert created.status == "open"
    assert created.checks[0].name == "schema"
    sent = route.calls.last.request
    assert sent.headers["idempotency-key"] == "k-1"
    assert json.loads(sent.content) == {
        "source_ref": "work",
        "target_ref": "main",
        "title": "Add suppliers",
    }


def test_list_proposals_filters_and_pages(http: respx.MockRouter, sleeps: Sleeps) -> None:
    route = http.get("/domains/supply/proposals").respond(
        200, json={"items": [_proposal()], "next_cursor": "c1"}
    )
    with make(sleeps) as client:
        result = client.proposals.list(
            "supply", status="open", origin="manual", source="work", target="main", limit=5
        )
    assert [p.id for p in result.items] == ["prp_1"]
    assert result.next_cursor == "c1"
    params = route.calls.last.request.url.params
    assert dict(params) == {
        "status": "open",
        "origin": "manual",
        "source": "work",
        "target": "main",
        "limit": "5",
    }


def test_proposal_reads_and_changes(http: respx.MockRouter, sleeps: Sleeps) -> None:
    get = http.get("/domains/supply/proposals/prp_1").respond(200, json=_detail())
    patch = http.patch("/domains/supply/proposals/prp_1").respond(200, json=_detail())
    close = http.post("/domains/supply/proposals/prp_1/close").respond(200, json=_detail())
    review = http.post("/domains/supply/proposals/prp_1/reviews").respond(
        201, json={"review": {**_review()}, "approval": _detail()["approval"]}
    )
    with make(sleeps) as client:
        assert client.proposals.get("supply", "prp_1").approval.satisfied
        client.proposals.update("supply", "prp_1", title="New title")
        client.proposals.close("supply", "prp_1")
        result = client.proposals.review("supply", "prp_1", "approve", comment="ok")
    assert get.called
    assert json.loads(patch.calls.last.request.content) == {"title": "New title"}
    assert close.calls.last.request.content == b""
    assert json.loads(review.calls.last.request.content) == {"verdict": "approve", "comment": "ok"}
    assert result.review.verdict == "approve"


def _review() -> dict[str, Any]:
    return {
        "id": "rev_1",
        "reviewer": "usr_2",
        "verdict": "approve",
        "comment": "ok",
        "at": "2026-10-01T09:00:00Z",
    }


def test_merge_proposal_sends_message_and_key(http: respx.MockRouter, sleeps: Sleeps) -> None:
    merged = {
        "status": "merged",
        "result": "merged",
        "revision": "dd",
        "proposal": _proposal(status="merged", merged_revision="dd"),
        "attempt": _conflicts(),
        "source_deleted": False,
        "replayed": False,
    }
    route = http.post("/domains/supply/proposals/prp_1/merge").respond(200, json=merged)
    with make(sleeps) as client:
        result = client.proposals.merge("supply", "prp_1", message="Ship it", idempotency_key="k-2")
    assert result.revision == "dd"
    assert result.proposal.status == "merged"
    sent = route.calls.last.request
    assert sent.headers["idempotency-key"] == "k-2"
    assert json.loads(sent.content) == {"message": "Ship it"}


def test_conflicts_are_paged_and_resolved(http: respx.MockRouter, sleeps: Sleeps) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.params["status"] == "open"
        cursor = request.url.params.get("cursor")
        return httpx.Response(200, json=_conflicts(None if cursor else "c1"))

    listing = http.get("/domains/supply/proposals/prp_1/conflicts").mock(side_effect=answer)
    one = http.post("/domains/supply/proposals/prp_1/conflicts/cf_1/resolution").respond(
        200,
        json={
            "fingerprint": FINGERPRINT,
            "choice": "custom",
            "value": {"x": 1},
            "resolver": "usr_1",
            "at": "2026-10-01T09:00:00Z",
        },
    )
    many = http.post("/domains/supply/proposals/prp_1/conflicts:resolve").respond(
        200, json=_conflicts()
    )
    with make(sleeps) as client:
        page = client.proposals.conflicts("supply", "prp_1", status="open", limit=1)
        assert page.next_cursor == "c1"
        assert page.conflicts[0].model_dump()["code"] == "DV1"
        keys = [c.id for c in client.proposals.iterate_conflicts("supply", "prp_1", status="open")]
        resolution = client.proposals.resolve("supply", "prp_1", "cf_1", "custom", value={"x": 1})
        client.proposals.resolve_many(
            "supply", "prp_1", [{"choice": "ours", "fingerprint": FINGERPRINT}]
        )
    assert keys == ["cf_1", "cf_1"]
    assert listing.call_count == 3
    assert resolution.value == {"x": 1}
    assert json.loads(one.calls.last.request.content) == {"choice": "custom", "value": {"x": 1}}
    assert json.loads(many.calls.last.request.content) == {
        "resolutions": [{"choice": "ours", "fingerprint": FINGERPRINT}]
    }


def test_resolutions_need_a_well_formed_fingerprint(sleeps: Sleeps) -> None:
    with make(sleeps) as client, pytest.raises(ValueError, match="fingerprint"):
        client.proposals.resolve_many("supply", "prp_1", [{"choice": "ours", "fingerprint": "x"}])


PACK = {
    "domain": "supply",
    "branch": "main",
    "revision": "aa",
    "etag": '"e"',
    "seeds": [{"kind": "Part", "key": "P-1"}],
    "neighbors": [],
    "links": {},
    "schema": {"kinds": {}},
    "budget": {"used": 10},
}


def test_context_json_and_markdown(http: respx.MockRouter, sleeps: Sleeps) -> None:
    route = http.post("/domains/supply/context")
    route.side_effect = [
        httpx.Response(200, json=PACK),
        httpx.Response(200, text="# Context\n", headers={"content-type": "text/markdown"}),
    ]
    with make(sleeps) as client:
        pack = client.agents.context("supply", {"refs": ["Part/P-1"], "depth": 1})
        text = client.agents.context_markdown("supply", m_context(search="bolt"))
    assert pack.schema_ == {"kinds": {}}
    assert pack.seeds[0]["key"] == "P-1"
    assert text == "# Context\n"
    first, second = (json.loads(c.request.content) for c in route.calls)
    assert first["format"] == "json"
    assert first["refs"] == ["Part/P-1"]
    assert second["format"] == "markdown"
    assert second["search"] == "bolt"
    assert route.calls[1].request.headers["accept"] == "text/markdown"


def m_context(**members: Any) -> Any:
    from lorebase_client.models import ContextRequest

    return ContextRequest(**members)


def test_explain_json_and_markdown(http: respx.MockRouter, sleeps: Sleeps) -> None:
    route = http.get("/domains/supply/kinds/Part/records/P-1/explain")
    explained = {
        "domain": "supply",
        "branch": "work",
        "revision": "aa",
        "record": {"kind": "Part", "key": "P-1"},
        "attributes": [],
        "links": [],
        "links_truncated": False,
        "history": {},
        "proposals": [],
    }
    route.side_effect = [
        httpx.Response(200, json=explained),
        httpx.Response(200, text="# Part P-1\n", headers={"content-type": "text/markdown"}),
    ]
    with make(sleeps) as client:
        result = client.agents.explain("supply", "Part", "P-1", branch="work")
        text = client.agents.explain_markdown("supply", "Part", "P-1", branch="work")
    assert result.record["key"] == "P-1"
    assert text.startswith("# Part")
    assert [dict(c.request.url.params) for c in route.calls] == [
        {"branch": "work", "format": "json"},
        {"branch": "work", "format": "markdown"},
    ]


def test_changes_feed_and_event_stream(http: respx.MockRouter, sleeps: Sleeps) -> None:
    event = {
        "type": "revision.committed",
        "domain": "supply",
        "branch": "work",
        "revision": "bb",
        "parents": ["aa"],
        "generation": 2,
        "author": "usr_1",
        "message": "m",
        "origin": {},
        "stats": {},
        "created_at": "2026-10-01T08:00:00Z",
        "kinds": {"Part": 1},
        "relations": {},
        "schema_changed": False,
        "keys": {"Part": ["P-1"]},
        "keys_truncated": False,
    }
    feed = http.get("/domains/supply/branches/agent/x/changes").respond(
        200,
        json={
            "branch": "agent/x",
            "head": "bb",
            "items": [event],
            "next_since": "bb",
            "more": False,
        },
    )
    stream = http.get("/domains/supply/branches/agent/x/events").respond(
        200, text="id: 1\ndata: {}\n\n", headers={"content-type": "text/event-stream"}
    )
    with make(sleeps) as client:
        page = client.agents.changes("supply", "agent/x", since="aa", kinds=["Part", "Link"])
        text = client.agents.events("supply", "agent/x", kinds=["Part"], last_event_id="1")
    assert page.items[0].keys == {"Part": ["P-1"]}
    assert page.more is False
    assert feed.calls.last.request.url.params.get_list("kinds") == ["Part", "Link"]
    assert feed.calls.last.request.url.params["since"] == "aa"
    assert text.startswith("id: 1")
    sent = stream.calls.last.request
    assert sent.headers["accept"] == "text/event-stream"
    assert dict(sent.url.params) == {"kinds": "Part", "last_event_id": "1"}


def test_webhooks_create_list_delete(http: respx.MockRouter, sleeps: Sleeps) -> None:
    hook = {
        "id": "wh_1",
        "url": "https://example.test/hook",
        "description": None,
        "events": ["revision.committed"],
        "branches": None,
        "created_by": "usr_1",
        "created_at": "2026-10-01T08:00:00Z",
    }
    create = http.post("/domains/supply/webhooks").respond(201, json={**hook, "secret": "whsec"})
    listing = http.get("/domains/supply/webhooks").respond(200, json={"items": [hook]})
    delete = http.delete("/domains/supply/webhooks/wh_1").respond(204)
    with make(sleeps) as client:
        created = client.webhooks.create(
            "supply", "https://example.test/hook", events=["revision.committed"]
        )
        hooks = client.webhooks.list("supply")
        assert client.webhooks.delete("supply", "wh_1") is None
    assert created.secret == "whsec"
    assert json.loads(create.calls.last.request.content) == {
        "url": "https://example.test/hook",
        "events": ["revision.committed"],
    }
    assert [h.id for h in hooks.items] == ["wh_1"]
    assert listing.called
    assert delete.called


def test_sources_sync_and_runs(http: respx.MockRouter, sleeps: Sleeps) -> None:
    queued = {"job_id": 7, "source": "src_1", "api_name": "erp.parts", "dry_run": True}
    run = {
        "id": "srn_1",
        "source": "src_1",
        "status": "succeeded",
        "started_at": "2026-10-01T08:00:00Z",
        "finished_at": "2026-10-01T08:00:30Z",
        "stats": {"counts": {"added": 2}},
        "revision": "aa",
        "proposal_id": None,
        "error": None,
    }
    sync = http.post("/domains/supply/sources/erp.parts/sync").respond(202, json=queued)
    runs = http.get("/domains/supply/sources/erp.parts/runs").respond(
        200, json={"items": [run], "next_cursor": None}
    )
    with make(sleeps) as client:
        job = client.sources.sync("supply", "erp.parts", dry_run=True)
        page = client.sources.runs("supply", "erp.parts", limit=5)
        listed = [r.id for r in client.sources.iterate_runs("supply", "erp.parts")]
    assert job.job_id == 7
    assert json.loads(sync.calls.last.request.content) == {"dry_run": True}
    assert page.items[0].stats == {"counts": {"added": 2}}
    assert runs.calls[0].request.url.params["limit"] == "5"
    assert listed == ["srn_1"]


DOCUMENT_ROW = {
    "id": "doc_1",
    "filename": "spec.txt",
    "media_type": "text/plain",
    "sha256": "ab" * 32,
    "size": 5,
    "text_sha256": None,
    "metadata": {},
    "created_at": "2026-10-01T08:00:00Z",
    "created_by": "usr_1",
}
ITEM_ROW = {
    "id": "itm_1",
    "type": "link",
    "seq": 1,
    "decision": "pending",
    "confidence": 0.9,
    "tier": 1,
    "model": "m",
    "relation": "supplies",
    "from": "S-1",
    "to": "P-1",
    "subject": {},
    "evidence": None,
    "match": None,
    "edits": None,
    "decided_by": None,
    "decided_at": None,
}


def test_documents_upload_sends_multipart_and_is_not_retried(
    http: respx.MockRouter, sleeps: Sleeps, tmp_path: Path
) -> None:
    answer = {"document": DOCUMENT_ROW, "duplicate": False, "job_id": 4}
    route = http.post("/domains/supply/documents").respond(201, json=answer)
    spec = tmp_path / "spec.txt"
    spec.write_bytes(b"hello")
    with make(sleeps) as client:
        stored = client.documents.upload("supply", spec, extract=False, tiers=[0, 1])
        client.documents.upload("supply", b"raw", filename="raw.bin")
    assert stored.job_id == 4
    assert stored.duplicate is False
    named, raw = (c.request for c in route.calls)
    assert named.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="spec.txt"' in named.content
    assert b"hello" in named.content
    assert b'name="extract"\r\n\r\nfalse' in named.content
    assert b'name="tiers"\r\n\r\n[0,1]' in named.content
    assert b'filename="raw.bin"' in raw.content
    assert b'name="extract"' not in raw.content
    with make(sleeps) as client, pytest.raises(ValueError, match="filename"):
        client.documents.upload("supply", b"raw")


def test_documents_upload_is_not_repeated_after_a_read_failure(
    http: respx.MockRouter, sleeps: Sleeps
) -> None:
    route = http.post("/domains/supply/documents").mock(side_effect=httpx.ReadTimeout("slow"))
    with make(sleeps) as client, pytest.raises(LorebaseConnectionError):
        client.documents.upload("supply", b"raw", filename="raw.bin")
    assert route.call_count == 1


def test_documents_upload_url_list_and_get(http: respx.MockRouter, sleeps: Sleeps) -> None:
    answer = {"document": DOCUMENT_ROW, "duplicate": True, "job_id": None}
    upload = http.post("/domains/supply/documents").respond(200, json=answer)
    listing = http.get("/domains/supply/documents").respond(
        200, json={"items": [DOCUMENT_ROW], "next_cursor": None}
    )
    job = {
        "id": "ext_1",
        "status": "succeeded",
        "model": "m",
        "prompt_version": "v1",
        "branch": "extraction/x",
        "proposal_id": "prp_1",
        "created_at": "2026-10-01T08:00:00Z",
        "finished_at": None,
        "usage": {},
        "stats": {"items": 3},
        "error": None,
    }
    http.get("/domains/supply/documents/doc_1").respond(
        200, json={**DOCUMENT_ROW, "extraction_jobs": [job]}
    )
    with make(sleeps) as client:
        again = client.documents.upload_url(
            "supply", "https://example.test/a.pdf", extract=True, tiers=(2,)
        )
        page = client.documents.list("supply", limit=5)
        listed = [d.id for d in client.documents.iterate("supply")]
        detail = client.documents.get("supply", "doc_1")
    assert again.duplicate is True
    assert json.loads(upload.calls.last.request.content) == {
        "url": "https://example.test/a.pdf",
        "extract": True,
        "tiers": [2],
    }
    assert page.items[0].filename == "spec.txt"
    assert listing.calls[0].request.url.params["limit"] == "5"
    assert listed == ["doc_1"]
    assert detail.extraction_jobs[0].proposal_id == "prp_1"


async def test_async_documents_upload_and_decide(http: respx.MockRouter, sleeps: Sleeps) -> None:
    upload = http.post("/domains/supply/documents").respond(
        201, json={"document": DOCUMENT_ROW, "duplicate": False, "job_id": None}
    )
    decided = {
        "proposal_id": "prp_1",
        "title": "Extraction",
        "checks": {},
        "items": [ITEM_ROW],
        "materialized": {"revision": None, "changes": 0, "applied": 0, "skipped": {}},
    }
    decide = http.post("/domains/supply/proposals/prp_1/items:decide").respond(200, json=decided)
    async with amake(sleeps) as client:
        stored = await client.documents.upload("supply", b"raw", filename="raw.bin")
        result = await client.proposals.decide_items(
            "supply", "prp_1", [m.ItemDecision(item="itm_1", decision="accepted")]
        )
    assert stored.document.id == "doc_1"
    assert b'filename="raw.bin"' in upload.calls.last.request.content
    assert result.items[0].from_key == "S-1"
    assert json.loads(decide.calls.last.request.content) == {
        "decisions": [{"item": "itm_1", "decision": "accepted"}]
    }


def test_proposal_items_filters_and_pages(http: respx.MockRouter, sleeps: Sleeps) -> None:
    route = http.get("/domains/supply/proposals/prp_1/items").mock(
        side_effect=[
            httpx.Response(200, json={"items": [ITEM_ROW], "next_cursor": "c2"}),
            httpx.Response(200, json={"items": [{**ITEM_ROW, "id": "itm_2"}], "next_cursor": None}),
        ]
    )
    with make(sleeps) as client:
        listed = list(
            client.proposals.iterate_items(
                "supply",
                "prp_1",
                status=["pending", "rejected"],
                kind=["supplies"],
                type=["link"],
                min_confidence=0.5,
                max_confidence=1.01,
                page_size=1,
            )
        )
    assert [i.id for i in listed] == ["itm_1", "itm_2"]
    assert listed[0].from_key == "S-1"
    assert listed[0].to_key == "P-1"
    first = route.calls[0].request.url
    assert first.params.get_list("status") == ["pending", "rejected"]
    assert first.params.get_list("kind") == ["supplies"]
    assert first.params.get_list("type") == ["link"]
    assert first.params["min_confidence"] == "0.5"
    assert first.params["max_confidence"] == "1.01"
    assert first.params["limit"] == "1"
    assert route.calls[1].request.url.params["cursor"] == "c2"


def test_proposal_items_without_filters_sends_none(http: respx.MockRouter, sleeps: Sleeps) -> None:
    route = http.get("/domains/supply/proposals/prp_1/items").respond(
        200, json={"items": [], "next_cursor": None}
    )
    with make(sleeps) as client:
        client.proposals.items("supply", "prp_1")
    assert dict(route.calls.last.request.url.params) == {}


def test_proposal_decide_items_sends_decisions_and_selection(
    http: respx.MockRouter, sleeps: Sleeps
) -> None:
    decided = {
        "proposal_id": "prp_1",
        "title": "Extraction",
        "checks": {"merge": {"conflicts": 0}},
        "items": [{**ITEM_ROW, "decision": "accepted"}],
        "materialized": {
            "revision": "aa",
            "changes": 2,
            "applied": 1,
            "skipped": {"itm_9": "stale"},
        },
    }
    route = http.post("/domains/supply/proposals/prp_1/items:decide").respond(200, json=decided)
    with make(sleeps) as client:
        result = client.proposals.decide_items(
            "supply",
            "prp_1",
            [
                m.ItemDecision(item="itm_1", decision="rejected", edits={}),
                {"item": "itm_2", "decision": "accepted"},
            ],
            where={"status": ["pending"], "min_confidence": 0.9},
            decision="accepted",
        )
    assert json.loads(route.calls.last.request.content) == {
        "decisions": [
            {"item": "itm_1", "decision": "rejected", "edits": {}},
            {"item": "itm_2", "decision": "accepted"},
        ],
        "where": {"status": ["pending"], "min_confidence": 0.9},
        "decision": "accepted",
    }
    assert result.materialized.skipped == {"itm_9": "stale"}
    assert result.items[0].decision == "accepted"
    with make(sleeps) as client, pytest.raises(ValidationError):
        client.proposals.decide_items("supply", "prp_1", where={"status": ["done"]})


def test_schema_inference_from_postgres_and_files(http: respx.MockRouter, sleeps: Sleeps) -> None:
    answer = {"domain": "supply", "status": "proposed", "yaml": "kinds: {}\n", "tables": []}
    route = http.post("/domains/supply/schema:infer").respond(200, json=answer)
    with make(sleeps) as client:
        result = client.schema.infer_postgres(
            "supply", "secret://env/ERP_DSN", tables=["parts"], options={"sample": 10}
        )
        client.schema.infer_files("supply", {"parts.csv": b"id\nP-1\n"}, options={"sample": 10})
    assert result["yaml"] == "kinds: {}\n"
    posted, uploaded = (c.request for c in route.calls)
    assert json.loads(posted.content) == {
        "postgres": {"dsn": "secret://env/ERP_DSN", "tables": ["parts"]},
        "options": {"sample": 10},
    }
    assert posted.url.params["format"] == "json"
    assert uploaded.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="files"; filename="parts.csv"' in uploaded.content
    assert b'name="options"' in uploaded.content
    assert b"id\nP-1\n" in uploaded.content


async def test_async_proposals_agents_and_webhooks(http: respx.MockRouter, sleeps: Sleeps) -> None:
    create = http.post("/domains/supply/proposals").respond(201, json=_detail())
    listing = http.get("/domains/supply/proposals").respond(
        200, json={"items": [_proposal()], "next_cursor": None}
    )
    conflicts = http.get("/domains/supply/proposals/prp_1/conflicts").respond(
        200, json=_conflicts()
    )
    context = http.post("/domains/supply/context").respond(200, json=PACK)
    hooks = http.get("/domains/supply/webhooks").respond(200, json={"items": []})
    async with amake(sleeps) as client:
        await client.proposals.create("supply", "work", "main", title="t", idempotency_key="k")
        listed = [p.id async for p in client.proposals.iterate("supply", status="open")]
        found = [c.id async for c in client.proposals.iterate_conflicts("supply", "prp_1")]
        pack = await client.agents.context("supply", {"search": "x"})
        empty = await client.webhooks.list("supply")
    assert create.calls.last.request.headers["idempotency-key"] == "k"
    assert listed == ["prp_1"]
    assert listing.calls.last.request.url.params["status"] == "open"
    assert found == ["cf_1"]
    assert conflicts.called
    assert pack.revision == "aa"
    assert json.loads(context.calls.last.request.content)["format"] == "json"
    assert empty.items == []
    assert hooks.called


SUMMARY = {
    "ok": False,
    "kinds": 1,
    "records": 3,
    "links": 2,
    "invalid_records": 1,
    "orphan_links": 0,
    "required_relation_gaps": 0,
    "duplicate_groups": 0,
    "stale_kinds": 0,
    "sampled_kinds": 0,
}


def test_health_reports_json_and_markdown(http: respx.MockRouter, sleeps: Sleeps) -> None:
    domain = http.get("/domains/supply/health")
    kind = http.get("/domains/supply/kinds/Part/health")
    head = {"revision": "aa", "served_from": "materialized", "domain": "supply", "branch": "work"}
    markdown = httpx.Response(200, text="# Health\n", headers={"content-type": "text/markdown"})
    domain.side_effect = [
        httpx.Response(
            200, json={**head, "summary": SUMMARY, "kinds": [{"kind": "Part"}], "relations": []}
        ),
        markdown,
    ]
    kind.side_effect = [
        httpx.Response(
            200, json={**head, "summary": SUMMARY, "health": {"kind": "Part"}, "relations": []}
        ),
        markdown,
    ]
    with make(sleeps) as client:
        report = client.agents.health("supply", branch="work")
        text = client.agents.health_markdown("supply", branch="work")
        one = client.agents.kind_health("supply", "Part", branch="work")
        one_text = client.agents.kind_health_markdown("supply", "Part", branch="work")
    assert not report.summary.ok
    assert report.summary.invalid_records == 1
    assert report.kinds[0]["kind"] == "Part"
    assert one.health["kind"] == "Part"
    assert text.startswith("# Health")
    assert one_text.startswith("# Health")
    assert [dict(c.request.url.params) for c in domain.calls] == [
        {"branch": "work", "format": "json"},
        {"branch": "work", "format": "markdown"},
    ]
    assert [dict(c.request.url.params) for c in kind.calls] == [
        {"branch": "work", "format": "json"},
        {"branch": "work", "format": "markdown"},
    ]


async def test_async_health_report(http: respx.MockRouter, sleeps: Sleeps) -> None:
    head = {"revision": "aa", "served_from": "materialized", "domain": "supply", "branch": "main"}
    http.get("/domains/supply/kinds/Part/health").respond(
        200, json={**head, "summary": SUMMARY, "health": {"kind": "Part"}, "relations": []}
    )
    async with amake(sleeps) as client:
        report = await client.agents.kind_health("supply", "Part")
    assert report.summary.records == 3
