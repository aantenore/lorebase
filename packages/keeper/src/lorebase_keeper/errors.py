"""Problem details (RFC 9457): the single error format of the API.

Every error response has the media type `application/problem+json` and the members `type`
(stable URI), `title`, `status`, `detail`, `instance` (the request path), `code` (the last part
of `type`), `request_id` and, when tracing is active, `trace_id`. Validation failures add
`errors`, a list of `{pointer, message, code}`. Responses never contain stack traces and never
echo request values.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException

PROBLEM_MEDIA_TYPE = "application/problem+json"


@dataclass(frozen=True)
class ProblemType:
    """A kind of error: stable code, HTTP status and short title."""

    code: str
    status: int
    title: str


BAD_REQUEST = ProblemType("bad-request", 400, "Bad request")
UNAUTHORIZED = ProblemType("unauthorized", 401, "Authentication required")
FORBIDDEN = ProblemType("forbidden", 403, "Forbidden")
NOT_FOUND = ProblemType("not-found", 404, "Not found")
METHOD_NOT_ALLOWED = ProblemType("method-not-allowed", 405, "Method not allowed")
CONFLICT = ProblemType("conflict", 409, "Conflict")
PAYLOAD_TOO_LARGE = ProblemType("payload-too-large", 413, "Request body too large")
VALIDATION_FAILED = ProblemType("validation-failed", 422, "Request validation failed")
RATE_LIMITED = ProblemType("rate-limited", 429, "Too many requests")
INTERNAL_ERROR = ProblemType("internal-error", 500, "Internal server error")
BAD_GATEWAY = ProblemType("bad-gateway", 502, "Bad gateway")
UNAVAILABLE = ProblemType("unavailable", 503, "Service unavailable")

_BY_STATUS = {
    p.status: p
    for p in (
        BAD_REQUEST,
        UNAUTHORIZED,
        FORBIDDEN,
        NOT_FOUND,
        METHOD_NOT_ALLOWED,
        CONFLICT,
        PAYLOAD_TOO_LARGE,
        VALIDATION_FAILED,
        RATE_LIMITED,
        INTERNAL_ERROR,
        BAD_GATEWAY,
        UNAVAILABLE,
    )
}


class ProblemError(HTTPException):
    """Raise from any route or dependency to answer with a problem detail.

    `extensions` adds members to the response body; `headers` adds response headers.
    """

    def __init__(
        self,
        problem: ProblemType,
        detail: str | None = None,
        *,
        extensions: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(
            status_code=problem.status, detail=detail, headers=dict(headers) if headers else None
        )
        self.problem = problem
        self.problem_detail = detail
        self.extensions = dict(extensions or {})


class FieldProblem(BaseModel):
    """One invalid request member."""

    pointer: str
    message: str
    code: str


class Problem(BaseModel):
    """Problem details document (schema of every error response)."""

    model_config = ConfigDict(extra="allow")

    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    code: str
    request_id: str | None = None
    trace_id: str | None = None


class ValidationProblem(Problem):
    """Problem details of a rejected request body, query or path."""

    errors: list[FieldProblem]


def current_trace_id() -> str | None:
    """Trace id of the active span, if tracing is recording one."""
    context = trace.get_current_span().get_span_context()
    return f"{context.trace_id:032x}" if context.is_valid else None


class ProblemRenderer:
    """Builds problem responses with the configured `type` URI base."""

    def __init__(self, type_base: str) -> None:
        self._type_base = type_base

    def type_uri(self, problem: ProblemType) -> str:
        """Stable URI identifying the kind of error."""
        return f"{self._type_base}{problem.code}"

    def body(
        self,
        problem: ProblemType,
        *,
        detail: str | None = None,
        instance: str | None = None,
        request_id: str | None = None,
        extensions: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The JSON document of a problem."""
        body: dict[str, Any] = {
            "type": self.type_uri(problem),
            "title": problem.title,
            "status": problem.status,
            "code": problem.code,
        }
        if detail:
            body["detail"] = detail
        if instance:
            body["instance"] = instance
        if request_id:
            body["request_id"] = request_id
        if trace_id := current_trace_id():
            body["trace_id"] = trace_id
        if extensions:
            body.update(extensions)
        return body

    def response(
        self,
        problem: ProblemType,
        *,
        detail: str | None = None,
        instance: str | None = None,
        request_id: str | None = None,
        extensions: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> JSONResponse:
        """A complete problem response."""
        return JSONResponse(
            self.body(
                problem,
                detail=detail,
                instance=instance,
                request_id=request_id,
                extensions=extensions,
            ),
            status_code=problem.status,
            headers=dict(headers) if headers else None,
            media_type=PROBLEM_MEDIA_TYPE,
        )


def request_id_of(scope_state: Mapping[str, Any] | None) -> str | None:
    """The request id stored in an ASGI scope's state by the request context middleware."""
    value = (scope_state or {}).get("request_id")
    return value if isinstance(value, str) else None


def _generic_problem(status: int) -> ProblemType:
    known = _BY_STATUS.get(status)
    if known is not None:
        return known
    return ProblemType(f"http-{status}", status, _phrase(status) or "Error")


def install_error_handlers(app: FastAPI, renderer: ProblemRenderer) -> None:
    """Route HTTP errors and request validation failures to problem details.

    Unhandled exceptions are answered by the request context middleware, which sits outside
    the framework's own error layer so that the response still carries the request id.
    """

    async def on_http_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, HTTPException)
        request_id = request_id_of(request.scope.get("state"))
        if isinstance(exc, ProblemError):
            return renderer.response(
                exc.problem,
                detail=exc.problem_detail,
                instance=request.url.path,
                request_id=request_id,
                extensions=exc.extensions,
                headers=exc.headers,
            )
        problem = _generic_problem(exc.status_code)
        raw: object = exc.detail  # FastAPI's subclass also allows non-string details
        detail = raw if isinstance(raw, str) and raw != _phrase(exc.status_code) else None
        return renderer.response(
            problem,
            detail=detail,
            instance=request.url.path,
            request_id=request_id,
            headers=exc.headers,
        )

    async def on_validation_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        errors = [
            {
                "pointer": "/" + "/".join(str(part) for part in error["loc"]),
                "message": str(error["msg"]),
                "code": str(error["type"]),
            }
            for error in exc.errors()
        ]
        return renderer.response(
            VALIDATION_FAILED,
            detail="One or more request members are invalid.",
            instance=request.url.path,
            request_id=request_id_of(request.scope.get("state")),
            extensions={"errors": errors},
        )

    app.add_exception_handler(HTTPException, on_http_error)
    app.add_exception_handler(RequestValidationError, on_validation_error)


def _phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ""
