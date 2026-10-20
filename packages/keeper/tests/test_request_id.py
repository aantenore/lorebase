from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager
from io import StringIO
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from lorebase_platform.logging import configure_logging
from lorebase_platform.settings import LoggingSettings, TelemetrySettings
from lorebase_platform.telemetry import Telemetry, setup_telemetry

ClientFor = Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_SPAN_ID = "00f067aa0ba902b7"


@pytest.fixture
def json_logs() -> Iterator[StringIO]:
    root = logging.getLogger()
    level = root.level
    stream = StringIO()
    handler = configure_logging(LoggingSettings(), service="keeper", stream=stream)
    yield stream
    root.removeHandler(handler)
    root.setLevel(level)


def access_records(stream: StringIO) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    return [r for r in records if r["logger"] == "lorebase.keeper.access"]


async def test_request_id_is_generated_when_absent(client: httpx.AsyncClient) -> None:
    first = await client.get("/healthz")
    second = await client.get("/healthz")
    assert len(first.headers["x-request-id"]) == 32
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


async def test_well_formed_request_id_is_propagated(
    client: httpx.AsyncClient, json_logs: StringIO
) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "client-req.42"})
    assert response.headers["x-request-id"] == "client-req.42"
    [record] = access_records(json_logs)
    assert record["request_id"] == "client-req.42"


async def test_request_id_reaches_error_bodies(client: httpx.AsyncClient) -> None:
    response = await client.get("/missing", headers={"X-Request-ID": "abc-1"})
    assert response.json()["request_id"] == "abc-1"


@pytest.mark.parametrize("value", ["x" * 200, "has space", "semi;colon"])
async def test_malformed_request_id_is_replaced(client: httpx.AsyncClient, value: str) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": value})
    assert response.headers["x-request-id"] != value
    assert len(response.headers["x-request-id"]) == 32


async def test_header_name_is_configurable(
    make_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    app = make_app({"request_id_header": "X-Correlation-ID"})
    async with client_for(app) as http:
        response = await http.get("/healthz", headers={"X-Correlation-ID": "corr-7"})
    assert response.headers["x-correlation-id"] == "corr-7"
    assert "x-request-id" not in response.headers


async def test_request_ids_do_not_leak_between_concurrent_requests(
    client: httpx.AsyncClient, json_logs: StringIO
) -> None:
    import asyncio

    ids = [f"req-{n}" for n in range(8)]
    await asyncio.gather(*(client.get("/healthz", headers={"X-Request-ID": i}) for i in ids))
    assert sorted(r["request_id"] for r in access_records(json_logs)) == sorted(ids)


async def test_access_log_has_route_status_and_duration_but_no_query(
    client: httpx.AsyncClient, json_logs: StringIO
) -> None:
    await client.get("/healthz?token=top-secret-value")
    [record] = access_records(json_logs)
    assert (record["method"], record["route"], record["status"]) == ("GET", "/healthz", 200)
    assert record["duration_ms"] >= 0
    assert "top-secret-value" not in json.dumps(access_records(json_logs))


async def test_unmatched_requests_are_logged_under_one_route(
    client: httpx.AsyncClient, json_logs: StringIO
) -> None:
    await client.get("/some/unknown/path/123")
    [record] = access_records(json_logs)
    assert record["route"] == "unmatched"
    assert record["status"] == 404


async def test_incoming_trace_context_is_continued(
    make_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    spans = InMemorySpanExporter()
    telemetry: Telemetry = setup_telemetry(
        TelemetrySettings(otlp_endpoint="http://collector.invalid:4317", sample_ratio=1.0),
        install_global=False,
        span_exporter=spans,
        metric_reader=InMemoryMetricReader(),
    )
    try:
        async with client_for(make_app(telemetry=telemetry)) as http:
            await http.get(
                "/healthz",
                headers={
                    "traceparent": f"00-{TRACE_ID}-{PARENT_SPAN_ID}-01",
                    "X-Request-ID": "traced-1",
                },
            )
        assert telemetry.tracer_provider is not None
        telemetry.tracer_provider.force_flush()
        [span] = spans.get_finished_spans()
    finally:
        telemetry.shutdown()

    assert span.name == "GET /healthz"
    assert span.context is not None
    assert span.parent is not None
    assert f"{span.context.trace_id:032x}" == TRACE_ID
    assert f"{span.parent.span_id:016x}" == PARENT_SPAN_ID
    assert span.attributes is not None
    assert span.attributes["lorebase.request_id"] == "traced-1"
    assert span.attributes["http.response.status_code"] == 200
    assert span.attributes["http.route"] == "/healthz"
