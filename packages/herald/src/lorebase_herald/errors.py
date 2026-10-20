"""Keeper problem details mapped to tool errors a model can act on.

A tool error is one short text: `[code] what happened` followed by `next:` with the action
that resolves it. `code` is Keeper's stable problem code (design 11.2: machine-readable code
and a short human message), or a Herald code for failures before Keeper answers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from lorebase_herald.keeper_http import KeeperError, KeeperUnavailableError

HintFactory = Callable[[KeeperError], str]


def _stale_head(error: KeeperError) -> str:
    current = error.extensions.get("current_head")
    return (
        f"the branch head is now {current}; re-read what the change depends on, then retry "
        f"with expected_head={current}"
        if current
        else "re-read the branch head and retry"
    )


def _review(error: KeeperError) -> str:
    proposals = error.extensions.get("proposals")
    where = f" ({proposals})" if proposals else ""
    return (
        "this change needs review: run it on a work branch, then open a Proposal for that "
        f"branch{where}; do not retry here"
    )


def _retry_later(error: KeeperError) -> str:
    after = error.retry_after or error.extensions.get("retry_after")
    return f"retry after {after} s" if after else "retry later"


HINTS: Mapping[str, HintFactory] = {
    "stale-head": _stale_head,
    "review-required": _review,
    "protected-branch": _review,
    "validation-failed": lambda _: "fix the listed members and call again",
    "not-found": lambda _: "check the ref with lorebase_search or names with lorebase_schema",
    "forbidden": lambda _: "this agent's API key lacks the permission; do not retry",
    "unauthorized": lambda _: "the agent's API key is missing, expired or revoked",
    "rate-limited": _retry_later,
    "branch-busy": _retry_later,
    "unavailable": _retry_later,
    "idempotency-conflict": lambda _: "use a new idempotency_key for different parameters",
    "expressions-not-enabled": lambda _: "this Operation cannot run on this deployment",
    "branch-not-materialized": lambda _: "read this branch at a Revision with `at`",
    "limit-exceeded": lambda _: "narrow the request",
}


def _issues(extensions: Mapping[str, Any], limit: int) -> list[str]:
    """Validation findings (`issues`) or request member errors (`errors`), one line each."""
    lines: list[str] = []
    found = extensions.get("issues") or extensions.get("errors") or []
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
    total = extensions.get("issue_count")
    if isinstance(total, int) and total > len(lines):
        lines.append(f"- ... {total - len(lines)} more")
    elif len(found) > limit:
        lines.append(f"- ... {len(found) - limit} more")
    return lines


def tool_error(error: KeeperError | KeeperUnavailableError, *, max_issues: int) -> ToolError:
    """The tool error answering a failed Keeper call."""
    if isinstance(error, KeeperUnavailableError):
        return ToolError(f"[keeper-unavailable] {error}. next: retry later")
    message = error.detail or error.title
    lines = [f"[{error.code}] {message}"]
    lines.extend(_issues(error.extensions, max_issues))
    hint = HINTS.get(error.code)
    if hint is None and error.status >= 500:
        hint = _retry_later
    if hint is not None:
        lines.append(f"next: {hint(error)}")
    return ToolError("\n".join(lines))


def invalid_input(code: str, message: str, hint: str) -> ToolError:
    """A tool error raised by Herald before calling Keeper."""
    return ToolError(f"[{code}] {message}\nnext: {hint}")
