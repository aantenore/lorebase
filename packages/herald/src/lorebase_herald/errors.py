"""Client errors mapped to tool errors a model can act on.

A tool error is one short text: `[code] what happened` followed by `next:` with the action
that resolves it. `code` is Keeper's stable problem code (design 11.2: machine-readable code
and a short human message), or a Herald code for failures before Keeper answers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from lorebase_client import (
    ApiError,
    AuthenticationError,
    IdempotencyConflictError,
    LorebaseConnectionError,
    NotFoundError,
    PermissionDeniedError,
    ProtectedBranchError,
    RateLimitedError,
    ReviewRequiredError,
    ServerError,
    StaleHeadError,
    ValidationFailedError,
)

HintFactory = Callable[[ApiError], str]


def _stale_head(error: ApiError) -> str:
    current = error.current_head
    return (
        f"the branch head is now {current}; re-read what the change depends on, then retry "
        f"with expected_head={current}"
        if current
        else "re-read the branch head and retry"
    )


def _review(error: ApiError) -> str:
    proposals = error.extra.get("proposals")
    where = f" ({proposals})" if proposals else ""
    return (
        "this change needs review: run it on a work branch, then open a Proposal for that "
        f"branch{where}; do not retry here"
    )


def _retry_later(error: ApiError) -> str:
    after = error.retry_after if error.retry_after is not None else error.extra.get("retry_after")
    if after is None:
        return "retry later"
    seconds = f"{after:g}" if isinstance(after, int | float) else after
    return f"retry after {seconds} s"


# Codes that no exception class of the client stands for.
HINTS_BY_CODE: Mapping[str, HintFactory] = {
    "expressions-not-enabled": lambda _: "this Operation cannot run on this deployment",
    "branch-not-materialized": lambda _: "read this branch at a Revision with `at`",
    "limit-exceeded": lambda _: "narrow the request",
}
# Most specific class first.
HINTS_BY_TYPE: tuple[tuple[type[ApiError], HintFactory], ...] = (
    (StaleHeadError, _stale_head),
    (ReviewRequiredError, _review),
    (ProtectedBranchError, _review),
    (IdempotencyConflictError, lambda _: "use a new idempotency_key for different parameters"),
    (ValidationFailedError, lambda _: "fix the listed members and call again"),
    (
        NotFoundError,
        lambda _: "check the ref with lorebase_search or names with lorebase_schema",
    ),
    (PermissionDeniedError, lambda _: "this agent's API key lacks the permission; do not retry"),
    (AuthenticationError, lambda _: "the agent's API key is missing, expired or revoked"),
    (RateLimitedError, _retry_later),
    (ServerError, _retry_later),
)


def _hint(error: ApiError) -> HintFactory | None:
    by_code = HINTS_BY_CODE.get(error.code or "")
    if by_code is not None:
        return by_code
    return next((hint for kind, hint in HINTS_BY_TYPE if isinstance(error, kind)), None)


def _issues(extra: Mapping[str, Any], limit: int) -> list[str]:
    """Validation findings (`issues`) or request member errors (`errors`), one line each."""
    lines: list[str] = []
    found = extra.get("issues") or extra.get("errors") or []
    if not isinstance(found, list):
        return lines
    for issue in found[:limit]:
        if not isinstance(issue, Mapping):
            continue
        where = issue.get("pointer") or "/".join(str(p) for p in issue.get("path") or [])
        code = issue.get("code")
        subject = "/".join(
            str(issue[k]) for k in ("kind", "key", "attribute") if issue.get(k) is not None
        )
        location = " ".join(part for part in (where, f"({subject})" if subject else "") if part)
        suffix = f" [{code}]" if code else ""
        lines.append(f"- {location or '<request>'}: {issue.get('message', '')}{suffix}")
    total = extra.get("issue_count")
    if isinstance(total, int) and total > len(lines):
        lines.append(f"- ... {total - len(lines)} more")
    elif len(found) > limit:
        lines.append(f"- ... {len(found) - limit} more")
    return lines


def tool_error(error: ApiError | LorebaseConnectionError, *, max_issues: int) -> ToolError:
    """The tool error answering a failed Keeper call."""
    if isinstance(error, LorebaseConnectionError):
        return ToolError(f"[keeper-unavailable] {_unreachable(error)}. next: retry later")
    # A problem always has a code; an answer without one is not Keeper's (a proxy, say) and its
    # body is no message for a model.
    message = (error.detail or error.title) if error.code else error.title
    lines = [f"[{error.code or f'http-{error.status}'}] {message or 'Error'}"]
    lines.extend(_issues(error.extra, max_issues))
    hint = _hint(error)
    if hint is not None:
        lines.append(f"next: {hint(error)}")
    return ToolError("\n".join(lines))


def _unreachable(error: LorebaseConnectionError) -> str:
    cause = error.__cause__
    if isinstance(cause, httpx.TimeoutException):
        return "Keeper did not answer in time"
    return f"Keeper is unreachable ({type(cause).__name__ if cause else 'no connection'})"


def _findings(error: ValidationError, limit: int) -> list[str]:
    found = error.errors(include_url=False, include_context=False, include_input=False)
    lines = [
        f"- {'/'.join(str(p) for p in finding['loc']) or '<request>'}: {finding['msg']} "
        f"[{finding['type']}]"
        for finding in found[:limit]
    ]
    if len(found) > limit:
        lines.append(f"- ... {len(found) - limit} more")
    return lines


def invalid_request(error: ValidationError, *, max_issues: int) -> ToolError:
    """The tool error for a request the client's models refuse before anything is sent."""
    lines = ["[validation-failed] the request is not valid", *_findings(error, max_issues)]
    lines.append("next: fix the listed members and call again")
    return ToolError("\n".join(lines))


def unexpected_answer(error: ValidationError) -> ToolError:
    """The tool error for an answer that does not fit the client's response models."""
    first = error.errors(include_url=False, include_context=False, include_input=False)[0]
    where = "/".join(str(p) for p in first["loc"])
    return ToolError(
        f"[keeper-response-invalid] Keeper's answer does not fit this Herald's API model "
        f"({error.error_count()} problems, first at {where}: {first['msg']}).\n"
        "next: use matching Herald and Keeper versions"
    )


def invalid_input(code: str, message: str, hint: str) -> ToolError:
    """A tool error raised by Herald before calling Keeper."""
    return ToolError(f"[{code}] {message}\nnext: {hint}")
