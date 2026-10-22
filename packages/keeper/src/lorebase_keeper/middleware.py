"""ASGI middleware: request context (id, tracing, timing, access log), body size limit and
security headers.

All are plain ASGI classes so that they see every request, including those rejected by the
framework, and so that streaming bodies are never buffered.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Sequence
from typing import Any

from opentelemetry import metrics, propagate, trace
from opentelemetry.metrics import MeterProvider
from opentelemetry.trace import SpanKind, Status, StatusCode, TracerProvider
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lorebase_keeper.client_address import ClientAddresses, parse_address
from lorebase_keeper.errors import (
    INTERNAL_ERROR,
    PAYLOAD_TOO_LARGE,
    ProblemError,
    ProblemRenderer,
    request_id_of,
)
from lorebase_keeper.settings import SecurityHeadersSettings
from lorebase_platform.logging import request_context
from lorebase_platform.telemetry import LATENCY_BUCKETS_S

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
                "http.server.request.duration",
                unit="s",
                description="Duration of HTTP requests.",
                explicit_bucket_boundaries_advisory=LATENCY_BUCKETS_S,
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
    bodies are counted as they are read. `overrides` give other limits to the requests whose
    method and path match (for example document uploads); the first match wins.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        renderer: ProblemRenderer,
        overrides: Sequence[tuple[str, re.Pattern[str], int]] = (),
    ) -> None:
        self.app = app
        self._max_bytes = max_bytes
        self._renderer = renderer
        self._overrides = tuple(overrides)

    def _limit(self, scope: Scope) -> int:
        for method, path, limit in self._overrides:
            if scope.get("method") == method and path.fullmatch(scope["path"]):
                return limit
        return self._max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self._limit(scope)
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdecimal() and int(declared) > limit:
            response = self._renderer.response(
                PAYLOAD_TOO_LARGE,
                detail=self._detail(limit),
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
                if received > limit:
                    raise ProblemError(PAYLOAD_TOO_LARGE, self._detail(limit))
            return message

        await self.app(scope, counted_receive, send)

    def _detail(self, limit: int) -> str:
        return f"The request body exceeds {limit} bytes."


class SecurityHeadersMiddleware:
    """Add the configured security headers to every response, keeping those a route set.

    `Strict-Transport-Security` follows `hsts.enabled`: in `auto` it is sent when the request
    came in over https, either directly or through a trusted proxy (`keeper.trusted_proxies`)
    whose last `X-Forwarded-Proto` value is https.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: SecurityHeadersSettings,
        proxies: ClientAddresses,
    ) -> None:
        self.app = app
        self._hsts = settings.hsts
        self._proxies = proxies
        static: list[tuple[str, str]] = []
        if settings.content_type_nosniff:
            static.append(("X-Content-Type-Options", "nosniff"))
        if settings.frame_options != "off":
            ancestors = "'none'" if settings.frame_options == "DENY" else "'self'"
            static.append(("X-Frame-Options", settings.frame_options))
            static.append(("Content-Security-Policy", f"frame-ancestors {ancestors}"))
        if settings.referrer_policy:
            static.append(("Referrer-Policy", settings.referrer_policy))
        self._static = tuple(static)

    def _secure(self, scope: Scope) -> bool:
        if scope["scheme"] in ("https", "wss"):
            return True
        client = scope.get("client")
        peer = parse_address(str(client[0])) if client else None
        if peer is None or not self._proxies.trusted(peer):
            return False
        forwarded = Headers(scope=scope).getlist("x-forwarded-proto")
        hops = [hop.strip().lower() for value in forwarded for hop in value.split(",")]
        return bool(hops) and hops[-1] == "https"

    def _send_hsts(self, scope: Scope) -> bool:
        mode = self._hsts.enabled
        return mode == "always" or (mode == "auto" and self._secure(scope))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        added = list(self._static)
        if self._send_hsts(scope):
            added.append(("Strict-Transport-Security", self._hsts.header_value))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in added:
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
