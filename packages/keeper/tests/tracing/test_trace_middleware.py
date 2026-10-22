"""Which requests are traced, what a step holds, and how the recorder writes and drops."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from starlette.datastructures import Headers

from lorebase_keeper.app import create_app
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.tracing.config import TraceSettings
from lorebase_keeper.tracing.middleware import TraceMiddleware, fingerprint
from lorebase_keeper.tracing.recorder import TraceRecorder, TraceSink
from lorebase_platform.settings import ConfigError, Settings
from lorebase_store.models.enums import PrincipalType
from lorebase_store.tracing import NewTraceStep

WHOAMI = "/api/v1/auth/whoami"


class Sink:
    def __init__(self, written: int | None = None) -> None:
        self.steps: list[NewTraceStep] = []
        self.pruned: list[datetime] = []
        self.fail = False
        self.written = written

    async def write(self, steps: Sequence[NewTraceStep]) -> int:
        if self.fail:
            raise RuntimeError("database down")
        self.steps.extend(steps)
        return len(steps) if self.written is None else self.written

    async def prune(self, before: datetime) -> int:
        self.pruned.append(before)
        return 0

    def sink(self) -> TraceSink:
        return TraceSink(self.write, self.prune)


def app_with(sink: Sink, **trace: Any) -> FastAPI:
    settings = Settings(keeper={"trace": {"enabled": True, "flush_interval_s": 0.01, **trace}})
    return create_app(settings, readiness_checks=[], trace_sink=sink.sink())


async def steps_of(app: FastAPI, sink: Sink, *requests: dict[str, Any]) -> list[NewTraceStep]:
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://keeper.test") as http,
    ):
        for request in requests:
            response = await http.request(**request)
            assert response.status_code != 500, response.text
        await app.state.trace.flush()
    return sink.steps


async def test_only_requests_that_opt_in_are_traced_and_explicit_sessions_win() -> None:
    sink = Sink()
    app = app_with(sink)
    steps = await steps_of(
        app,
        sink,
        {"method": "GET", "url": WHOAMI},
        {"method": "GET", "url": WHOAMI, "headers": {"X-Lorebase-Session": "s-1"}},
        {
            "method": "GET",
            "url": WHOAMI,
            "headers": {"X-Lorebase-Trace": "on", "User-Agent": "agent/1"},
        },
        {
            "method": "GET",
            "url": WHOAMI,
            "headers": {"X-Lorebase-Session": "bad session", "X-Lorebase-Trace": "on"},
        },
        {
            "method": "GET",
            "url": WHOAMI,
            "headers": {
                "X-Lorebase-Session": "s-1",
                "X-Lorebase-Trace": "on",
                "X-Lorebase-Tool": "lorebase_whoami",
            },
        },
        {
            "method": "GET",
            "url": "/api/v1/trace/sessions",
            "headers": {"X-Lorebase-Session": "s-1", "X-Lorebase-Trace": "on"},
        },
    )
    inferred, step = steps
    assert (inferred.session_id, inferred.inferred) == ("", True)
    assert inferred.fingerprint
    assert (step.session_id, step.actor, step.type) == ("s-1", "anonymous", "call")
    assert (step.inferred, step.fingerprint) == (False, None)
    assert (step.method, step.route, step.status) == ("GET", WHOAMI, 200)
    assert step.tool == "lorebase_whoami"
    assert step.latency_ms is not None
    assert step.request_id


async def test_listed_principals_and_trace_all_need_no_opt_in() -> None:
    for trace in ({"principals": ["anonymous"]}, {"trace_all": True}):
        sink = Sink()
        app = app_with(sink, allow_header_opt_in=False, **trace)
        steps = await steps_of(
            app, sink, {"method": "GET", "url": WHOAMI, "headers": {"X-Lorebase-Session": "s-2"}}
        )
        assert [s.session_id for s in steps] == ["s-2"]
    sink = Sink()
    app = app_with(sink, allow_header_opt_in=False, principals=["usr_other"])
    headers = {"X-Lorebase-Session": "s-3", "X-Lorebase-Trace": "on"}
    assert await steps_of(app, sink, {"method": "GET", "url": WHOAMI, "headers": headers}) == []


async def test_notes_are_queued_and_bounded() -> None:
    sink = Sink()
    app = app_with(sink, max_note_chars=10)
    steps = await steps_of(
        app,
        sink,
        {"method": "POST", "url": "/api/v1/trace/sessions/s-4/notes", "json": {"text": "why"}},
        {"method": "POST", "url": "/api/v1/trace/sessions/s-4/notes", "json": {"text": "x" * 11}},
        {"method": "POST", "url": "/api/v1/trace/sessions/s%204/notes", "json": {"text": "x"}},
    )
    [note] = steps
    assert (note.type, note.note, note.session_id, note.domain_id) == ("note", "why", "s-4", None)


def test_tracing_needs_a_database() -> None:
    with pytest.raises(ConfigError, match=r"keeper\.trace\.enabled"):
        create_app(Settings(keeper={"trace": {"enabled": True}}), readiness_checks=[])


async def test_recorder_drops_and_prunes() -> None:
    reader = InMemoryMetricReader()
    sink = Sink(written=1)
    settings = TraceSettings(enabled=True, queue_size=2, batch_size=5, flush_interval_s=0.01)
    recorder = TraceRecorder(
        settings, sink.sink(), meter_provider=MeterProvider(metric_readers=[reader])
    )
    step = NewTraceStep(session_id="s", actor="a", type="call", at=datetime.now(UTC))
    assert [recorder.offer(step) for _ in range(3)] == [True, True, False]
    await recorder.flush()
    sink.fail = True
    assert recorder.offer(step)
    await recorder.flush()
    await recorder.aclose()
    assert sink.pruned  # the pruner ran once on start
    dropped = {
        point.attributes["reason"]: point.value
        for metric in reader.get_metrics_data().resource_metrics[0].scope_metrics[0].metrics
        if metric.name == "lorebase.trace.steps.dropped"
        for point in metric.data.data_points
    }
    assert dropped == {"queue_full": 1, "session_full": 1, "write_failed": 1}


def test_fingerprints_separate_principals_credentials_and_agents() -> None:
    def principal(id_: str, key: str | None) -> Principal:
        return Principal(
            id=id_,
            type=PrincipalType.SERVICE,
            display_name=id_,
            method="api_key",
            subjects=(id_,),
            key_id=key,
        )

    agent = Headers({"user-agent": "agent/1"})
    same = fingerprint(principal("svc_a", "key_1"), agent, None)
    assert same == fingerprint(principal("svc_a", "key_1"), agent, None)
    assert same != fingerprint(principal("svc_b", "key_1"), agent, None)
    assert same != fingerprint(principal("svc_a", "key_2"), agent, None)
    assert same != fingerprint(principal("svc_a", "key_1"), Headers({"user-agent": "x"}), None)
    web = principal("usr_a", None)
    assert fingerprint(web, agent, "cookie-1") != fingerprint(web, agent, "cookie-2")


async def test_trace_all_leaves_out_the_web_app_unless_included() -> None:
    web = {"X-Lorebase-Client": "Web"}
    requests = (
        {"method": "GET", "url": WHOAMI, "headers": web},
        {"method": "GET", "url": WHOAMI, "headers": {**web, "X-Lorebase-Trace": "on"}},
        {"method": "GET", "url": WHOAMI, "headers": {"X-Lorebase-Client": "herald"}},
        {"method": "GET", "url": WHOAMI, "headers": {"X-Lorebase-Session": "guide-1"}},
    )
    sink = Sink()
    steps = await steps_of(app_with(sink, trace_all=True), sink, *requests)
    assert len(steps) == 3  # the opt-in, Herald and the named session; not the screens
    sink = Sink()
    steps = await steps_of(app_with(sink, trace_all=True, include_web=True), sink, *requests)
    assert len(steps) == 4
    sink = Sink()
    listed = app_with(sink, principals=["anonymous"])
    assert len(await steps_of(listed, sink, requests[0])) == 1  # listing is explicit


def test_session_cookie_traffic_without_an_agent_session_is_the_web_app() -> None:
    settings = TraceSettings(enabled=True, trace_all=True)
    middleware = TraceMiddleware(FastAPI(), recorder=TraceRecorder(settings, Sink().sink()))

    def user(method: Any) -> Principal:
        return Principal(
            id="usr_a",
            type=PrincipalType.USER,
            display_name="usr_a",
            method=method,
            subjects=("usr_a",),
        )

    assert not middleware._traced(user("session"), Headers({}), opted_in=False)
    guide = Headers({"x-lorebase-session": "guide-1"})
    assert middleware._traced(user("session"), guide, opted_in=False)
    assert middleware._traced(user("oidc"), Headers({}), opted_in=False)
    assert middleware._traced(user("session"), Headers({}), opted_in=True)
