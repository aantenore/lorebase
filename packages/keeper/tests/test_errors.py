from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager
from io import StringIO
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from lorebase_keeper.errors import CONFLICT, ProblemError
from lorebase_platform.logging import configure_logging
from lorebase_platform.settings import LoggingSettings, TelemetrySettings
from lorebase_platform.telemetry import Telemetry, setup_telemetry

ClientFor = Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]
PROBLEM = "application/problem+json"


class Item(BaseModel):
    name: str


@pytest.fixture
def json_logs() -> Iterator[StringIO]:
    root = logging.getLogger()
    level = root.level
    stream = StringIO()
    handler = configure_logging(LoggingSettings(level="DEBUG"), service="keeper", stream=stream)
    yield stream
    root.removeHandler(handler)
    root.setLevel(level)


@pytest.fixture
def telemetry() -> Iterator[Telemetry]:
    handle = setup_telemetry(
        TelemetrySettings(otlp_endpoint="http://collector.invalid:4317", sample_ratio=1.0),
        install_global=False,
        span_exporter=InMemorySpanExporter(),
        metric_reader=InMemoryMetricReader(),
    )
    yield handle
    handle.shutdown()


@pytest.fixture
def api(app: FastAPI) -> FastAPI:
    async def boom() -> None:
        raise RuntimeError("internal detail that must not leak")

    async def create_item(item: Item) -> Item:
        return item

    async def echo(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    async def conflict() -> None:
        raise ProblemError(
            CONFLICT, "Branch main moved.", extensions={"expected": "a1", "actual": "b2"}
        )

    app.add_api_route("/boom", boom)
    app.add_api_route("/items", create_item, methods=["POST"])
    app.add_api_route("/echo", echo, methods=["POST"])
    app.add_api_route("/conflict", conflict)
    return app


def log_records(stream: StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


async def test_unknown_path_is_a_problem_detail(client: httpx.AsyncClient) -> None:
    response = await client.get("/nope")
    body = response.json()
    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM
    assert body["type"] == "urn:lorebase:problem:not-found"
    assert body["title"] == "Not found"
    assert body["status"] == 404
    assert body["code"] == "not-found"
    assert body["instance"] == "/nope"
    assert body["request_id"] == response.headers["x-request-id"]
    assert "detail" not in body


async def test_wrong_method_keeps_allow_header(client: httpx.AsyncClient) -> None:
    response = await client.post("/healthz")
    assert response.status_code == 405
    assert response.json()["code"] == "method-not-allowed"
    assert "GET" in response.headers["allow"]


async def test_raised_problem_error_carries_extensions(api: FastAPI, client_for: ClientFor) -> None:
    async with client_for(api) as http:
        response = await http.get("/conflict")
    body = response.json()
    assert response.status_code == 409
    assert response.headers["content-type"] == PROBLEM
    assert body["type"] == "urn:lorebase:problem:conflict"
    assert body["detail"] == "Branch main moved."
    assert (body["expected"], body["actual"]) == ("a1", "b2")


async def test_type_uri_base_is_configurable(
    make_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    app = make_app({"problem_type_base": "https://errors.example.com/"})
    async with client_for(app) as http:
        response = await http.get("/nope")
    assert response.json()["type"] == "https://errors.example.com/not-found"


async def test_validation_errors_list_members_without_echoing_values(
    api: FastAPI, client_for: ClientFor
) -> None:
    async with client_for(api) as http:
        response = await http.post("/items", json={"name": ["leaky-value"]})
    body = response.json()
    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM
    assert body["code"] == "validation-failed"
    assert body["errors"][0]["pointer"] == "/body/name"
    assert set(body["errors"][0]) == {"pointer", "message", "code"}
    assert "leaky-value" not in response.text


async def test_unhandled_exception_returns_problem_without_stack_trace(
    api: FastAPI, client_for: ClientFor, json_logs: StringIO
) -> None:
    async with client_for(api) as http:
        response = await http.get("/boom")
    body = response.json()
    assert response.status_code == 500
    assert response.headers["content-type"] == PROBLEM
    assert body["type"] == "urn:lorebase:problem:internal-error"
    assert body["request_id"] == response.headers["x-request-id"]
    assert "Traceback" not in response.text
    assert "internal detail" not in response.text
    assert "RuntimeError" not in response.text

    [failure] = [r for r in log_records(json_logs) if r["message"].startswith("unhandled")]
    assert failure["request_id"] == body["request_id"]
    assert "internal detail that must not leak" in failure["exception"]


async def test_trace_id_is_in_the_problem_and_in_the_error_log(
    make_app: Callable[..., FastAPI],
    client_for: ClientFor,
    json_logs: StringIO,
    telemetry: Telemetry,
) -> None:
    app = make_app(telemetry=telemetry)

    async def boom() -> None:
        raise RuntimeError("x")

    app.add_api_route("/boom", boom)
    async with client_for(app) as http:
        response = await http.get("/boom")
    trace_id = response.json()["trace_id"]
    assert len(trace_id) == 32
    [failure] = [r for r in log_records(json_logs) if r["message"].startswith("unhandled")]
    assert failure["trace_id"] == trace_id


async def test_error_after_response_start_is_logged_and_status_kept(
    app: FastAPI, client_for: ClientFor, json_logs: StringIO
) -> None:
    from starlette.responses import StreamingResponse

    async def stream() -> AsyncIterator[bytes]:
        yield b"first"
        raise RuntimeError("mid-stream failure")

    async def streaming() -> StreamingResponse:
        return StreamingResponse(stream())

    app.add_api_route("/stream", streaming)
    async with client_for(app) as http:
        response = await http.get("/stream")
    assert response.status_code == 200
    assert any(r["message"].startswith("unhandled") for r in log_records(json_logs))


async def test_declared_oversized_body_is_rejected_before_the_handler(
    api: FastAPI, make_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    app = make_app({"limits": {"max_request_body_bytes": 16}})
    called = False

    async def echo() -> None:
        nonlocal called
        called = True

    app.add_api_route("/echo", echo, methods=["POST"])
    async with client_for(app) as http:
        response = await http.post("/echo", content=b"x" * 17)
    assert response.status_code == 413
    assert response.json()["code"] == "payload-too-large"
    assert response.headers["x-request-id"]
    assert not called


async def test_streamed_oversized_body_is_rejected_while_reading(
    make_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    app = make_app({"limits": {"max_request_body_bytes": 16}})

    async def echo(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    app.add_api_route("/echo", echo, methods=["POST"])

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(4):
            yield b"x" * 8

    async with client_for(app) as http:
        rejected = await http.post("/echo", content=chunks())
        accepted = await http.post("/echo", content=b"x" * 16)
    assert rejected.status_code == 413
    assert rejected.headers["content-type"] == PROBLEM
    assert accepted.json() == {"bytes": 16}


async def test_openapi_metadata_and_problem_schema(client: httpx.AsyncClient) -> None:
    document = (await client.get("/api/v1/openapi.json")).json()
    assert document["openapi"].startswith("3.1")
    assert document["info"]["title"] == "Lorebase Keeper"
    assert document["info"]["version"]
    assert {"/healthz", "/readyz"} <= set(document["paths"])
    assert "Problem" in document["components"]["schemas"]
    docs = await client.get("/api/v1/docs")
    assert docs.status_code == 200
    assert "text/html" in docs.headers["content-type"]


async def test_openapi_follows_prefix_and_can_be_disabled(
    make_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    async with client_for(make_app({"api_prefix": "/v2"})) as http:
        assert (await http.get("/v2/openapi.json")).status_code == 200
        assert (await http.get("/api/v1/openapi.json")).status_code == 404
    async with client_for(make_app({"openapi": {"docs_enabled": False}})) as http:
        assert (await http.get("/api/v1/openapi.json")).status_code == 404
        assert (await http.get("/api/v1/docs")).status_code == 404
        assert (await http.get("/healthz")).status_code == 200


def test_auth_mode_without_its_settings_is_rejected_at_startup(
    make_app: Callable[..., FastAPI],
) -> None:
    from lorebase_platform.settings import ConfigError

    with pytest.raises(ConfigError, match=r"keeper\.auth"):
        make_app({"auth": {"mode": "oidc"}})
