"""Agent trace headers on the Keeper calls of each tool."""

from __future__ import annotations

import hashlib
import re

from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from lorebase_herald.settings import TraceSettings
from lorebase_herald.tracing import (
    TRACEPARENT as W3C_TRACEPARENT,
)
from lorebase_herald.tracing import (
    Tracing,
    call_headers,
    call_span,
    request_traceparent,
    tool_span,
    trace_id_of,
)

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
    tracing.begin(None, "lorebase_search")
    assert call_span.get() is not None
    span = tool_span(None, incoming)
    assert span.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert span.parent_id == "00f067aa0ba902b7"  # the caller's span is its parent
    assert span.span_id != span.parent_id  # a span of its own
    assert trace_id_of("default") == trace_id_of("default")
    # keyed per process: not the plain digest of the session id
    assert trace_id_of("default") != hashlib.sha256(b"default").hexdigest()[:32]


def parts(traceparent: str) -> tuple[str, str]:
    _, trace_id, span_id, _ = traceparent.split("-")
    return trace_id, span_id


def test_requests_of_one_tool_call_are_child_spans_with_fresh_ids() -> None:
    tracing().begin(None, "lorebase_get")
    tool = call_span.get()
    assert tool is not None
    sent = [request_traceparent() for _ in range(2)]
    assert all(sent)
    (trace_one, span_one), (trace_two, span_two) = (parts(v) for v in sent if v)
    assert trace_one == trace_two == tool.trace_id
    assert len({span_one, span_two, tool.span_id}) == 3
    assert tool.child().parent_id == tool.span_id


async def test_each_keeper_request_has_its_own_span(connect: Connect, fake: FakeKeeper) -> None:
    fake.on("GET", f"{API}/schema", SCHEMA)
    async with connect(trace={"session_id": "run-7"}) as client:
        await client.call_tool("lorebase_schema", {})
        await client.call_tool("lorebase_schema", {})
    spans = [parts(r.headers["traceparent"]) for r in fake.requests]
    assert {t for t, _ in spans} == {trace_id_of("run-7")}
    assert len({s for _, s in spans}) == len(spans)


def test_no_request_span_when_tracing_is_off() -> None:
    tracing(enabled=False).begin(None, "lorebase_get")
    assert request_traceparent() is None


def test_active_opentelemetry_span_is_propagated() -> None:
    context = SpanContext(
        trace_id=0x4BF92F3577B34DA6A3CE929D0E0E4736,
        span_id=0x00F067AA0BA902B7,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    with trace.use_span(NonRecordingSpan(context)):
        tracing().begin(None, "lorebase_get")
        tool = call_span.get()
        sent = [request_traceparent() for _ in range(2)]
    assert tool is not None
    assert tool.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert tool.span_id == "00f067aa0ba902b7"
    spans = [W3C_TRACEPARENT.fullmatch(value or "") for value in sent]
    assert all(m is not None and m.group(1) == tool.trace_id for m in spans)
    ids = {m.group(2) for m in spans if m is not None}
    assert len(ids) == 2  # each request is a child span of its own
    assert tool.span_id not in ids


def test_active_opentelemetry_span_is_not_sent_when_tracing_is_off() -> None:
    context = SpanContext(
        trace_id=0x4BF92F3577B34DA6A3CE929D0E0E4736,
        span_id=0x00F067AA0BA902B7,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    with trace.use_span(NonRecordingSpan(context)):
        tracing(enabled=False).begin(None, "lorebase_get")
        assert request_traceparent() is None
