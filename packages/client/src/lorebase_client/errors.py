"""Typed exceptions raised for failed requests.

Every Keeper error is a problem detail (RFC 9457, media type `application/problem+json`).
`error_from_response` maps one to a `LorebaseError` subclass: first by the problem `code`
(`stale-head`, `protected-branch`, ...), then by HTTP status. Members beyond the standard ones
stay available in `extra` (`issues`, `errors`, `current_head`, `permission`, ...) and through
the convenience properties.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

_STANDARD = frozenset(
    {"type", "title", "status", "detail", "instance", "code", "request_id", "trace_id"}
)
_SNIPPET_CHARS = 300


class LorebaseError(Exception):
    """Base class of every error raised by the client."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        type: str | None = None,
        title: str | None = None,
        detail: str | None = None,
        code: str | None = None,
        instance: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        extra: Mapping[str, Any] | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.type = type
        self.title = title
        self.detail = detail
        self.code = code
        self.instance = instance
        self.request_id = request_id
        self.trace_id = trace_id
        self.extra: dict[str, Any] = dict(extra or {})
        self.retry_after = retry_after

    @property
    def issues(self) -> list[dict[str, Any]]:
        """Validation findings of a rejected write (`issues`), each with code, path, message."""
        return list(self.extra.get("issues") or [])

    @property
    def errors(self) -> list[dict[str, Any]]:
        """Invalid request members (`errors`): `{pointer, message, code}` each."""
        return list(self.extra.get("errors") or [])

    @property
    def current_head(self) -> str | None:
        """Branch head reported by a `stale-head` conflict."""
        value = self.extra.get("current_head")
        return None if value is None else str(value)

    def __str__(self) -> str:
        if self.request_id:
            return f"{self.message} (request {self.request_id})"
        return self.message


class LorebaseConnectionError(LorebaseError):
    """The server could not be reached or the connection failed (no HTTP response)."""


class LorebaseConfigError(LorebaseError):
    """The client is not configured (for example no base URL)."""


class ApiError(LorebaseError):
    """The server answered with an error status."""


class BadRequestError(ApiError):
    """400."""


class AuthenticationError(ApiError):
    """401: no valid credential."""


class PermissionDeniedError(ApiError):
    """403: `extra['permission']` names the missing permission."""


class NotFoundError(ApiError):
    """404."""


class ConflictError(ApiError):
    """409: the state of the server does not allow the request."""


class StaleHeadError(ConflictError):
    """409 `stale-head`: the branch moved; `current_head` is where it is now."""


class ProtectedBranchError(ConflictError):
    """409 `protected-branch`: changes reach the branch through a Proposal."""


class ReviewRequiredError(ConflictError):
    """409 `review-required`: the change needs a Proposal."""


class AlreadyExistsError(ConflictError):
    """409 `already-exists`."""


class IdempotencyConflictError(ConflictError):
    """409 `idempotency-conflict`: the key was used with other parameters."""


class PayloadTooLargeError(ApiError):
    """413."""


class ValidationFailedError(ApiError):
    """422: invalid request (`errors`) or data that breaks the schema (`issues`)."""


class RateLimitedError(ApiError):
    """429; `retry_after` holds the wait the server asked for, in seconds."""


class ServerError(ApiError):
    """5xx."""


class UnavailableError(ServerError):
    """503."""


class BranchBusyError(UnavailableError):
    """503 `branch-busy`: another writer holds the branch; retry later."""


_BY_CODE: dict[str, type[ApiError]] = {
    "stale-head": StaleHeadError,
    "protected-branch": ProtectedBranchError,
    "review-required": ReviewRequiredError,
    "already-exists": AlreadyExistsError,
    "idempotency-conflict": IdempotencyConflictError,
    "branch-busy": BranchBusyError,
}
_BY_STATUS: dict[int, type[ApiError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    413: PayloadTooLargeError,
    422: ValidationFailedError,
    429: RateLimitedError,
    503: UnavailableError,
}


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Seconds to wait from a `Retry-After` header (delta-seconds or HTTP date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, moment.timestamp() - (time.time() if now is None else now))


def _problem_of(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = json.loads(response.content)
    except ValueError:
        return None
    return body if isinstance(body, dict) and "status" in body else None


def is_problem(response: httpx.Response) -> bool:
    """True when the response is a problem document (so the server itself rejected the call)."""
    return _problem_of(response) is not None


def error_from_response(response: httpx.Response) -> ApiError:
    """The exception for an error response."""
    status = response.status_code
    retry_after = parse_retry_after(response.headers.get("retry-after"))
    problem = _problem_of(response)
    if problem is None:
        snippet = response.text.strip()[:_SNIPPET_CHARS]
        reason = response.reason_phrase or "error"
        message = f"HTTP {status} {reason}" + (f": {snippet}" if snippet else "")
        return _class_for(None, status)(
            message, status=status, title=reason, detail=snippet or None, retry_after=retry_after
        )
    code = _text(problem.get("code"))
    detail = _text(problem.get("detail"))
    title = _text(problem.get("title"))
    return _class_for(code, status)(
        detail or title or f"HTTP {status}",
        status=status,
        type=_text(problem.get("type")),
        title=title,
        detail=detail,
        code=code,
        instance=_text(problem.get("instance")),
        request_id=_text(problem.get("request_id")),
        trace_id=_text(problem.get("trace_id")),
        extra={k: v for k, v in problem.items() if k not in _STANDARD},
        retry_after=retry_after,
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _class_for(code: str | None, status: int) -> type[ApiError]:
    if code is not None and code in _BY_CODE:
        return _BY_CODE[code]
    if status in _BY_STATUS:
        return _BY_STATUS[status]
    return ServerError if status >= 500 else ApiError
