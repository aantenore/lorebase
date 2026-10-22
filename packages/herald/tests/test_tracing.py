"""Agent trace headers on the Keeper calls of each tool."""

from __future__ import annotations

import hashlib
import re

from lorebase_herald.settings import TraceSettings
from lorebase_herald.tracing import Tracing, call_headers, trace_id_of

from .conftest import Connect
from .fake_keeper import API, SCHEMA, FakeKeeper

TRACEPARENT = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-01$")


def tracing(**overrides: object) -> Tracing:
    return Tracing(TraceSettings.model_validate(overrides))


async def test_without_a_derivable_session_no_session_header_is_sent(
    connect: Connect, fake: FakeKeeper
) -> None:
    fake.on("GET", f"{API}/schema", SCHEMA)
    async with connect() as client:
        await client.call_tool("lorebase_schema", {})
        await client.call_tool("lorebase_schema", {})
    first, second = fake.requests
    assert first.headers["x-lorebase-tool"] == "lorebase_schema"
    assert "x-lorebase-session" not in first.headers
    assert "x-lorebase-trace" not in first.headers
    assert TRACEPARENT.match(first.headers["traceparent"])
    assert first.headers["traceparent"] != second.headers["traceparent"]


async def test_session_calls_share_a_trace_with_a_fresh_span_each(
    connect: Connect, fake: FakeKeeper
) -> None:
    fake.on("GET", f"{API}/schema", SCHEMA)
    async with connect(trace={"session_id": "run-7"}) as client:
        await client.call_tool("lorebase_schema", {})
        await client.call_tool("lorebase_schema", {})
    first, second = fake.requests
    assert first.headers["x-lorebase-session"] == second.headers["x-lorebase-session"] == "run-7"
    one, two = first.headers["traceparent"], second.headers["traceparent"]
    assert one.split("-")[1] == two.split("-")[1] == trace_id_of("run-7")
    assert one.split("-")[2] != two.split("-")[2]


async def test_configured_session_and_opt_in(connect: Connect, fake: FakeKeeper) -> None:
    fake.on("GET", f"{API}/schema", SCHEMA)
    async with connect(trace={"session_id": "run-7", "opt_in": True}) as client:
        await client.call_tool("lorebase_schema", {})
    sent = fake.last().headers
    assert sent["x-lorebase-session"] == "run-7"
    assert sent["x-lorebase-trace"] == "on"
    assert sent["traceparent"].split("-")[1] == trace_id_of("run-7")


async def test_disabled_tracing_sends_no_headers(connect: Connect, fake: FakeKeeper) -> None:
    fake.on("GET", f"{API}/schema", SCHEMA)
    async with connect(trace={"enabled": False, "opt_in": True}) as client:
        await client.call_tool("lorebase_schema", {})
    sent = set(fake.last().headers)
    assert not {"x-lorebase-session", "x-lorebase-tool", "x-lorebase-trace", "traceparent"} & sent


def test_session_id_order_of_sources() -> None:
    plain = tracing()
    assert plain.session_id(None) is None
    assert plain.session_id({}) is None
    mcp = {"mcp-session-id": "abc123"}
    assert plain.session_id(mcp) == "abc123"
    assert plain.session_id({**mcp, "X-Lorebase-Session": "caller-1"}) == "caller-1"
    pinned = tracing(session_id="pinned")
    assert pinned.session_id(mcp) == "pinned"
    assert pinned.session_id({"x-lorebase-session": "caller-1"}) == "caller-1"


def test_invalid_incoming_ids_are_ignored() -> None:
    bad = {"x-lorebase-session": "has space", "mcp-session-id": "x" * 129}
    assert tracing().session_id(bad) is None


def test_incoming_trace_mode_is_forwarded_and_custom_names_used() -> None:
    custom = tracing(session_header="X-S", tool_header="X-T", trace_header="X-Tr")
    out = custom.headers("lorebase_get", {"x-tr": "Debug", "X-S": "s-1"})
    assert out["X-S"] == "s-1"
    assert out["X-T"] == "lorebase_get"
    assert out["X-Tr"] == "debug"
    assert "X-Tr" not in custom.headers("lorebase_get", {"x-tr": "bogus"})


def test_begin_without_a_request_sends_no_session() -> None:
    tracing().begin(None, "lorebase_get")
    headers = call_headers.get() or {}
    assert headers["X-Lorebase-Tool"] == "lorebase_get"
    assert "X-Lorebase-Session" not in headers


def test_incoming_traceparent_is_continued_and_session_trace_ids_are_keyed() -> None:
    tracing = Tracing(TraceSettings())
    incoming = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    sent = tracing.headers("lorebase_search", {"traceparent": incoming})["traceparent"]
    assert sent.startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    assert sent != incoming  # a span of its own
    assert trace_id_of("default") == trace_id_of("default")
    # keyed per process: not the plain digest of the session id
    assert trace_id_of("default") != hashlib.sha256(b"default").hexdigest()[:32]
