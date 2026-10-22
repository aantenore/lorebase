"""Agent trace: session headers on requests and the `trace` resource."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError

from lorebase_client import AsyncLorebaseClient, LorebaseClient, LorebaseConfigError
from lorebase_client import models as m

BASE = "http://keeper.test"
API = f"{BASE}/api/v1"

SESSION = {
    "session_id": "s-1",
    "actor": "usr_1",
    "started_at": "2026-10-01T08:00:00Z",
    "last_at": "2026-10-01T08:05:00Z",
    "calls": 3,
    "notes": 1,
    "domains": ["supply"],
    "tools": ["search"],
    "proposals": [],
}
TIMELINE = {
    "session_id": "s-1",
    "truncated": False,
    "steps": [
        {
            "step": 1,
            "seq": 10,
            "type": "call",
            "at": "2026-10-01T08:00:01Z",
            "actor": "usr_1",
            "tool": "search",
            "status": 200,
            "refs": {
                "records": [{"kind": "Part", "key": "P-1"}],
                "links": [
                    {
                        "relation": "supplied_by",
                        "from": {"kind": "Part", "key": "P-1"},
                        "to": {"kind": "Supplier", "key": "S-1"},
                    }
                ],
                "truncated": False,
            },
        },
        {
            "step": 2,
            "seq": 11,
            "type": "note",
            "at": "2026-10-01T08:00:02Z",
            "actor": "usr_1",
            "note": "<b>checked</b>",
        },
    ],
}


@pytest.fixture
def http() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


def client(**options: Any) -> LorebaseClient:
    return LorebaseClient(BASE, api_key="lbk_test", env={}, **options)


def test_session_options_become_headers(http: respx.MockRouter) -> None:
    route = http.get("/domains").respond(200, json={"items": []})
    with client(session_id="s-1", tool="search", trace="debug") as c:
        c.domains.list()
    sent = route.calls.last.request.headers
    assert sent["x-lorebase-session"] == "s-1"
    assert sent["x-lorebase-tool"] == "search"
    assert sent["x-lorebase-trace"] == "debug"


def test_event_hooks_run_on_every_request(http: respx.MockRouter) -> None:
    route = http.get("/domains").respond(200, json={"items": []})
    count = 0

    def stamp(request: httpx.Request) -> None:
        nonlocal count
        count += 1
        request.headers["traceparent"] = f"hook-{count}"

    with client(event_hooks={"request": [stamp]}) as c:
        c.domains.list()
        c.domains.list()
    assert [call.request.headers["traceparent"] for call in route.calls] == ["hook-1", "hook-2"]


async def test_async_event_hooks_run_on_every_request(http: respx.MockRouter) -> None:
    route = http.get("/domains").respond(200, json={"items": []})

    async def stamp(request: httpx.Request) -> None:
        request.headers["x-hooked"] = "yes"

    async with AsyncLorebaseClient(
        BASE, api_key="lbk_test", env={}, event_hooks={"request": [stamp]}
    ) as c:
        await c.domains.list()
    assert route.calls.last.request.headers["x-hooked"] == "yes"


def test_no_session_options_send_no_trace_headers(http: respx.MockRouter) -> None:
    route = http.get("/domains").respond(200, json={"items": []})
    with client() as c:
        c.domains.list()
    sent = route.calls.last.request.headers
    assert not {"x-lorebase-session", "x-lorebase-tool", "x-lorebase-trace"} & set(sent)


@pytest.mark.parametrize("bad", ["has space", "x" * 129, "a/b"])
def test_session_id_shape_is_validated(bad: str) -> None:
    with pytest.raises(LorebaseConfigError):
        client(session_id=bad)
    with client() as c, pytest.raises(LorebaseConfigError):
        c.with_session(bad)


def test_with_session_derives_a_client_on_the_same_connection(http: respx.MockRouter) -> None:
    route = http.get("/domains").respond(200, json={"items": []})
    with client(session_id="base", tool="a", trace="on") as c:
        other = c.with_session("other", tool="b")
        other.domains.list()
        assert route.calls.last.request.headers["x-lorebase-session"] == "other"
        assert route.calls.last.request.headers["x-lorebase-tool"] == "b"
        assert route.calls.last.request.headers["x-lorebase-trace"] == "on"
        c.domains.list()
        assert route.calls.last.request.headers["x-lorebase-session"] == "base"
        assert route.calls.last.request.headers["x-lorebase-tool"] == "a"
        assert other.trace is not c.trace


def test_trace_sessions_and_timeline(http: respx.MockRouter) -> None:
    listing = http.get("/trace/sessions").respond(
        200, json={"enabled": True, "items": [SESSION], "next_cursor": None}
    )
    detail = http.get("/trace/sessions/s-1").respond(200, json=TIMELINE)
    with client() as c:
        page = c.trace.sessions(actor="usr_1", domain_id="supply", limit=5)
        timeline = c.trace.session("s-1", actor="usr_1")
    assert page.enabled
    assert page.items[0].calls == 3
    query = dict(listing.calls.last.request.url.params)
    assert query == {"actor": "usr_1", "domain_id": "supply", "limit": "5"}
    assert dict(detail.calls.last.request.url.params) == {"actor": "usr_1"}
    first, second = timeline.steps
    assert first.refs is not None
    assert first.refs.links[0].source.key == "P-1"
    assert second.note == "<b>checked</b>"


def test_iterate_follows_cursors(http: respx.MockRouter) -> None:
    http.get("/trace/sessions").mock(
        side_effect=[
            respx.MockResponse(200, json={"enabled": True, "items": [SESSION], "next_cursor": "c"}),
            respx.MockResponse(200, json={"enabled": True, "items": [SESSION]}),
        ]
    )
    with client() as c:
        assert len(list(c.trace.iterate())) == 2


def test_add_note_posts_text(http: respx.MockRouter) -> None:
    route = http.post("/trace/sessions/s-1/notes").respond(202, json={"recorded": True})
    with client() as c:
        accepted = c.trace.add_note("s-1", "why", domain_id="supply", step=4)
    assert accepted.recorded
    assert json.loads(route.calls.last.request.content) == {
        "text": "why",
        "domain_id": "supply",
        "step": 4,
    }


def test_search_tuning_is_sent_and_validated(http: respx.MockRouter) -> None:
    route = http.post("/domains/supply/search").respond(
        200,
        json={
            "branch": "main",
            "revision": "r",
            "served_from": "tree",
            "mode": "lexical",
            "items": [],
        },
    )
    request = m.SearchRequest(q="x", tuning=m.SearchTuning(rrf_k=30, lexical_weight=2))
    with client() as c:
        c.records.search("supply", request)
    assert json.loads(route.calls.last.request.content)["tuning"] == {
        "rrf_k": 30,
        "lexical_weight": 2.0,
    }
    with pytest.raises(ValidationError):
        m.SearchTuning(trigram_threshold=0.01)


@pytest.mark.asyncio
async def test_async_client_headers_and_resource(http: respx.MockRouter) -> None:
    http.get("/trace/sessions/s-1").respond(200, json=TIMELINE)
    note = http.post("/trace/sessions/s-1/notes").respond(202, json={"recorded": False})
    async with AsyncLorebaseClient(
        BASE, api_key="lbk_test", env={}, session_id="s-1", tool="t"
    ) as c:
        timeline = await c.trace.session("s-1")
        derived = c.with_session("s-2")
        result = await derived.trace.add_note("s-1", "n")
    assert len(timeline.steps) == 2
    assert not result.recorded
    sent = note.calls.last.request.headers
    assert sent["x-lorebase-session"] == "s-2"
    assert sent["x-lorebase-tool"] == "t"
