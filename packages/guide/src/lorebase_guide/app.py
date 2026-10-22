"""The Guide HTTP service.

- `POST /embed` `{text, mode: query|document}` -> `{model, dims, vector, mode}`;
- `POST /explore` `{domain, branch?, question, max_steps?, budget?}` -> server-sent events:
  `session`, then `thought`, `tool_call` and `tool_result` per step, then `answer` (or `error`
  followed by `answer`), and `done`;
- `GET /healthz` (liveness) and `GET /readyz` (Keeper reachable).

Every request is authenticated with the caller's own credential and checked by Keeper before
any model work (see `auth.py`); limits per credential protect the model budget (`limits.py`).
Errors are problem documents (`application/problem+json`) with a `code`.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lorebase_client.errors import LorebaseError
from lorebase_guide import __version__
from lorebase_guide.agent import Budget, Exploration, Explorer
from lorebase_guide.auth import AuthRefusedError, Caller, read_caller, verify
from lorebase_guide.embed import Embedder
from lorebase_guide.keeper import KeeperClients
from lorebase_guide.limits import Gate, Refusal, SlidingWindow, Slots
from lorebase_guide.settings import GuideSettings
from lorebase_guide.telemetry import (
    EMBEDDINGS,
    EXPLORATIONS,
    REFUSED,
    REQUEST_DURATION,
    REQUESTS,
    tracer,
)
from lorebase_guide.trace import TraceSession, new_session_id
from lorebase_inference import ChatModel, InferenceError, InferenceSettings, create_chat_model
from lorebase_platform.logging import request_context

logger = logging.getLogger("lorebase.guide")
access_logger = logging.getLogger("lorebase.guide.access")

NAME = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
BRANCH = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$"
PROBLEM = "application/problem+json"


class EmbedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, description="Text to embed.")
    mode: Literal["query", "document"] = Field(
        default="query",
        description="query: inference.embeddings.query_prefix is put before the text; "
        "document: document_prefix, as Scribe does for Record texts.",
    )


class EmbedResponse(BaseModel):
    model: str = Field(description="Model name vectors are stored under (Keeper's model).")
    dims: int
    mode: Literal["query", "document"]
    vector: list[float]


class ExploreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: Annotated[str, Field(pattern=NAME, description="Domain id.")]
    branch: Annotated[str | None, Field(default=None, pattern=BRANCH)] = None
    question: str = Field(min_length=1)
    max_steps: int | None = Field(
        default=None, ge=1, le=50, description="Lower than guide.agent.max_steps only."
    )
    budget: int | None = Field(
        default=None,
        ge=100,
        description="Token budget; lower than guide.agent.max_total_tokens only.",
    )


def problem(
    status: int, code: str, detail: str, *, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        {"type": "about:blank", "title": code, "status": status, "code": code, "detail": detail},
        status_code=status,
        media_type=PROBLEM,
        headers=headers,
    )


def _refused(refusal: Refusal) -> JSONResponse:
    REFUSED.add(1, {"reason": refusal.reason})
    detail = {
        "rate": "too many requests from this caller; retry later",
        "concurrent": "this caller already has an exploration running",
        "busy": "Guide is busy; retry later",
    }.get(refusal.reason, "refused")
    return problem(429, refusal.reason, detail, headers={"Retry-After": str(refusal.retry_after_s)})


def _auth_refused(exc: AuthRefusedError) -> JSONResponse:
    REFUSED.add(1, {"reason": exc.code})
    headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
    return problem(exc.status, exc.code, exc.message, headers=headers)


def sse(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


class _EventStream(StreamingResponse):
    """A stream whose resources (concurrency slot, Keeper client) are released however the
    response ends, also when the client goes away before the first event."""

    def __init__(
        self,
        content: AsyncIterator[bytes],
        cleanup: Callable[[], Awaitable[None]],
        **kwargs: Any,
    ) -> None:
        super().__init__(content, **kwargs)
        self._cleanup = cleanup

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._cleanup()


@dataclass
class Runtime:
    settings: GuideSettings
    keepers: KeeperClients
    embedder: Embedder
    chat: Callable[[], ChatModel]
    explore_window: SlidingWindow
    embed_window: SlidingWindow
    slots: Slots
    verifying: Gate
    _explorer: Explorer | None = None

    def explorer(self) -> Explorer:
        if self._explorer is None:
            self._explorer = Explorer(self.chat(), self.settings.agent, self.embedder)
        return self._explorer


class AccessMiddleware:
    """Request id, server span, body size limit, timing and an access log line without
    values (method, route, status, duration)."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self._app = app
        self._max_body = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        path = scope.get("path", "")
        started = time.monotonic()
        status = 500

        async def tracked_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                headers.append((b"x-content-type-options", b"nosniff"))
                message = {**message, "headers": headers}
            await send(message)

        declared = _content_length(scope)
        refusal: JSONResponse | None = None
        if scope.get("method") == "POST":
            if declared is None:
                refusal = problem(411, "length_required", "send the body with a Content-Length")
            elif declared > self._max_body:
                refusal = problem(413, "payload_too_large", "request body too large")
        with (
            request_context(request_id),
            tracer.start_as_current_span(
                f"{scope.get('method', '')} {path}", kind=SpanKind.SERVER
            ) as span,
        ):
            try:
                if refusal is not None:
                    await refusal(scope, receive, tracked_send)
                else:
                    await self._app(scope, _capped(receive, self._max_body), tracked_send)
            finally:
                elapsed = time.monotonic() - started
                span.set_attribute("http.response.status_code", status)
                route = path if path in _ROUTES else "other"
                REQUESTS.add(1, {"route": route, "status": status})
                REQUEST_DURATION.record(elapsed, {"route": route})
                access_logger.info(
                    "request",
                    extra={
                        "method": scope.get("method"),
                        "route": route,
                        "status": status,
                        "duration_ms": round(elapsed * 1000),
                    },
                )


def _capped(receive: Receive, limit: int) -> Receive:
    """`receive` that ends the body once `limit` bytes were read (a Content-Length that lies
    cannot make the service read more)."""
    received = 0

    async def capped() -> Message:
        nonlocal received
        message = await receive()
        if message["type"] == "http.request":
            body = message.get("body", b"")
            if received + len(body) > limit:
                body = body[: max(0, limit - received)]
                message = {"type": "http.request", "body": body, "more_body": False}
            received += len(body)
        return message

    return capped


_ROUTES = frozenset({"/embed", "/explore", "/healthz", "/readyz"})


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _headers(request: Request) -> dict[str, str]:
    return {k.lower(): v for k, v in request.headers.items()}


def create_app(
    settings: GuideSettings,
    inference: InferenceSettings,
    *,
    chat: Callable[[], ChatModel] | None = None,
    embedder: Embedder | None = None,
    keeper_transport: Any = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """The Guide application. `chat`, `embedder` and `keeper_transport` replace the configured
    model, embedding model and network (tests)."""
    runtime = Runtime(
        settings=settings,
        keepers=KeeperClients(settings, transport=keeper_transport),
        embedder=embedder or Embedder(inference),
        chat=chat or (lambda: create_chat_model(inference, task=settings.agent.task)),
        explore_window=SlidingWindow(
            settings.limits.explorations_per_minute,
            clock,
            max_keys=settings.limits.max_tracked_callers,
        ),
        embed_window=SlidingWindow(
            settings.limits.embeds_per_minute,
            clock,
            max_keys=settings.limits.max_tracked_callers,
        ),
        slots=Slots(settings.limits.max_concurrent, settings.limits.max_concurrent_per_caller),
        verifying=Gate(settings.limits.max_concurrent_verifications),
    )
    app = FastAPI(
        title="Lorebase Guide",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime = runtime
    http = settings.http

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'][1:]) or 'body'}: {e['msg']}"
            for e in exc.errors()[:5]
        )
        return problem(422, "invalid_request", problems[:500])

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        ok = await runtime.keepers.probe(settings.readiness_timeout_s)
        body = {"status": "ready" if ok else "not_ready", "checks": {"keeper": ok}}
        return JSONResponse(body, status_code=200 if ok else 503)

    async def _verified(caller: Caller) -> Caller | JSONResponse:
        if not runtime.verifying.enter():
            return _refused(Refusal("busy", 1))
        try:
            async with runtime.keepers.open(caller) as client:
                return await verify(caller, client)
        except AuthRefusedError as exc:
            return _auth_refused(exc)
        except LorebaseError:
            return problem(502, "keeper_unavailable", "Keeper could not be reached")
        finally:
            runtime.verifying.leave()

    @app.post("/embed", response_model=EmbedResponse)
    async def embed(body: EmbedRequest, request: Request) -> Response:
        try:
            caller = read_caller(_headers(request), http)
        except AuthRefusedError as exc:
            return _auth_refused(exc)
        if len(body.text) > settings.limits.max_embed_chars:
            return problem(422, "text_too_long", "text is longer than allowed")
        verified = await _verified(caller)
        if isinstance(verified, JSONResponse):
            return verified
        refusal = runtime.embed_window.take(verified.identity)
        if refusal is not None:
            return _refused(refusal)
        try:
            result = await runtime.embedder.embed(body.text, body.mode)
        except InferenceError as exc:
            logger.warning("embedding failed", extra={"error": type(exc).__name__})
            return problem(502, "embedding_failed", "the embedding model failed")
        EMBEDDINGS.add(1, {"mode": body.mode})
        payload = EmbedResponse(
            model=result.model, dims=result.dims, mode=body.mode, vector=result.vector
        )
        return JSONResponse(payload.model_dump(), headers={"Cache-Control": "no-store"})

    @app.post("/explore")
    async def explore(body: ExploreRequest, request: Request) -> Response:
        try:
            caller = read_caller(_headers(request), http)
        except AuthRefusedError as exc:
            return _auth_refused(exc)
        if len(body.question) > settings.agent.max_question_chars:
            return problem(422, "question_too_long", "the question is longer than allowed")
        if not runtime.verifying.enter():
            return _refused(Refusal("busy", 1))
        stack = AsyncExitStack()
        try:
            trace = TraceSession(settings.trace, new_session_id(), body.domain)
            client = await stack.enter_async_context(runtime.keepers.open(caller, trace.headers()))
            caller = await verify(caller, client)
            explorer = runtime.explorer()
        except AuthRefusedError as exc:
            await stack.aclose()
            return _auth_refused(exc)
        except LorebaseError:
            await stack.aclose()
            return problem(502, "keeper_unavailable", "Keeper could not be reached")
        except BaseException:
            await stack.aclose()
            raise
        finally:
            runtime.verifying.leave()
        # Limits count verified callers only, keyed on the principal Keeper reported.
        key = caller.identity
        refusal = runtime.explore_window.take(key) or runtime.slots.acquire(key)
        if refusal is not None:
            await stack.aclose()
            return _refused(refusal)
        stack.callback(runtime.slots.release, key)
        budget = Budget.of(settings.agent, max_steps=body.max_steps, budget=body.budget)
        state = Exploration()

        async def stream() -> AsyncGenerator[bytes]:
            started = time.monotonic()
            try:
                async for event in explorer.run(
                    client=client,
                    trace=trace,
                    domain=body.domain,
                    branch=body.branch,
                    question=body.question,
                    budget=budget,
                    state=state,
                ):
                    yield sse(event.name, event.data)
            finally:
                if state.outcome == "running":
                    state.outcome = "aborted"
                EXPLORATIONS.add(1, {"outcome": state.outcome})
                logger.info(
                    "exploration finished",
                    extra={
                        "session": trace.session_id,
                        "outcome": state.outcome,
                        "steps": state.steps,
                        "input_tokens": state.usage.input_tokens,
                        "output_tokens": state.usage.output_tokens,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    },
                )
                await stack.aclose()

        events = stream()

        async def cleanup() -> None:
            await events.aclose()
            await stack.aclose()

        return _EventStream(
            events,
            cleanup,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(http.allowed_origins),
        allow_methods=["GET", "POST"],
        allow_headers=[http.credential_header, http.csrf_header, "Content-Type"],
        allow_credentials=bool(http.session_cookie),
        max_age=600,
    )
    app.add_middleware(AccessMiddleware, max_body_bytes=http.max_body_bytes)
    return app
