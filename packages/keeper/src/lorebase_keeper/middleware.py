"""ASGI middleware: request context (id, tracing, timing, access log) and body size limit.

Both are plain ASGI classes so that they see every request, including those rejected by the
framework, and so that streaming bodies are never buffered.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

from opentelemetry import metrics, propagate, trace
from opentelemetry.metrics import MeterProvider
from opentelemetry.trace import SpanKind, Status, StatusCode, TracerProvider
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lorebase_keeper.errors import (
    INTERNAL_ERROR,
    PAYLOAD_TOO_LARGE,
    ProblemError,
    ProblemRenderer,
    request_id_of,
)
from lorebase_platform.logging import request_context

logger = logging.getLogger("lorebase.keeper")
access_logger = logging.getLogger("lorebase.keeper.access")

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
UNMATCHED_ROUTE = "unmatched"


def new_request_id() -> str:
    """A fresh request id."""
    return uuid.uuid4().hex


class RequestContextMiddleware:
    """Assign the request id, open the server span, time the request and log it.

    The request id comes from the configured header when it is well formed, otherwise a new
    one is generated; it is echoed in the response header, bound to every log record of the
    request and attached to the span. Incoming W3C trace context is continued. Unhandled
    exceptions are logged with their stack trace and answered with a generic problem detail.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        header: str,
        renderer: ProblemRenderer,
        tracer_provider: TracerProvider | None = None,
        meter_provider: MeterProvider | None = None,
    ) -> None:
        self.app = app
        self._header = header
        self._renderer = renderer
        self._tracer = (tracer_provider or trace.get_tracer_provider()).get_tracer(
            "lorebase.keeper"
        )
        self._duration = (
            (meter_provider or metrics.get_meter_provider())
            .get_meter("lorebase.keeper")
            .create_histogram(
                "http.server.request.duration", unit="s", description="Duration of HTTP requests."
            )
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        candidate = headers.get(self._header)
        request_id = candidate if candidate and _SAFE_REQUEST_ID.match(candidate) else None
        request_id = request_id or new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id
        method = scope["method"]
        status = 500
        started = False

        async def send_with_id(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                started = True
                status = message["status"]
                MutableHeaders(scope=message)[self._header] = request_id
            await send(message)

        begin = time.perf_counter()
        with (
            request_context(request_id),
            self._tracer.start_as_current_span(
                method,
                context=propagate.extract(dict(headers)),
                kind=SpanKind.SERVER,
                attributes={"http.request.method": method, "url.path": scope["path"]},
            ) as span,
        ):
            span.set_attribute("lorebase.request_id", request_id)
            try:
                await self.app(scope, receive, send_with_id)
            except Exception as exc:
                logger.exception("unhandled error while serving request", extra={"method": method})
                span.record_exception(exc)
                if not started:
                    response = self._renderer.response(
                        INTERNAL_ERROR,
                        detail=f"Unexpected error. Quote request id {request_id} when reporting.",
                        instance=scope["path"],
                        request_id=request_id,
                    )
                    await response(scope, receive, send_with_id)
                else:
                    raise  # the status line is gone: let the server abort the connection
            finally:
                elapsed = time.perf_counter() - begin
                route = _route_template(scope)
                span.update_name(f"{method} {route}")
                span.set_attribute("http.route", route)
                span.set_attribute("http.response.status_code", status)
                if status >= 500:
                    span.set_status(Status(StatusCode.ERROR))
                self._duration.record(
                    elapsed,
                    {
                        "http.request.method": method,
                        "http.route": route,
                        "http.response.status_code": status,
                    },
                )
                access_logger.info(
                    "request completed",
                    extra={
                        "method": method,
                        "route": route,
                        "status": status,
                        "duration_ms": round(elapsed * 1000, 3),
                    },
                )


def _route_template(scope: Scope) -> str:
    route: Any = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else UNMATCHED_ROUTE


class BodyLimitMiddleware:
    """Reject request bodies above the configured size with 413.

    A declared `Content-Length` over the limit is refused before the handler runs; chunked
    bodies are counted as they are read.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int, renderer: ProblemRenderer) -> None:
        self.app = app
        self._max_bytes = max_bytes
        self._renderer = renderer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdecimal() and int(declared) > self._max_bytes:
            response = self._renderer.response(
                PAYLOAD_TOO_LARGE,
                detail=self._detail(),
                instance=scope["path"],
                request_id=request_id_of(scope.get("state")),
            )
            await response(scope, receive, send)
            return

        received = 0

        async def counted_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    raise ProblemError(PAYLOAD_TOO_LARGE, self._detail())
            return message

        await self.app(scope, counted_receive, send)

    def _detail(self) -> str:
        return f"The request body exceeds {self._max_bytes} bytes."
