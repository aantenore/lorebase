"""Problem types of the write endpoints and the mapping of store failures to them.

Store errors become problem details with stable codes:

| Failure | Status | Code | Extra members |
|---------|--------|------|---------------|
| `StaleHeadError` | 409 | `stale-head` | `branch`, `expected_head`, `current_head` |
| `BranchLockTimeoutError` | 503 | `branch-busy` | `Retry-After` header |
| `ValidationFailedError` | 422 | `validation-failed` | `issues` |
| `SchemaChangeError` | 422 | `schema-change-invalid` | |
| `BranchNotFoundError` | 404 | `not-found` | |
| `BranchNotMaterializedError` | 409 | `branch-not-materialized` | |

A lock timeout is transient (another writer holds the branch), so it is a 503 with a retry
hint rather than 423, which clients rarely handle.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from lorebase_core.canonical import hex
from lorebase_core.validate import Issue
from lorebase_keeper.errors import NOT_FOUND, VALIDATION_FAILED, ProblemError, ProblemType
from lorebase_store.commit import (
    BranchLockTimeoutError,
    BranchNotFoundError,
    BranchNotMaterializedError,
    CommitError,
    SchemaChangeError,
    StaleHeadError,
    ValidationFailedError,
)

STALE_HEAD = ProblemType("stale-head", 409, "Branch head moved")
BRANCH_BUSY = ProblemType("branch-busy", 503, "Branch is busy")
BRANCH_NOT_MATERIALIZED = ProblemType("branch-not-materialized", 409, "Branch not materialized")
SCHEMA_CHANGE_INVALID = ProblemType("schema-change-invalid", 422, "Schema change not applicable")
PROTECTED_BRANCH = ProblemType("protected-branch", 409, "Branch is protected")
REVIEW_REQUIRED = ProblemType("review-required", 409, "Review required")
EXPRESSIONS_NOT_ENABLED = ProblemType("expressions-not-enabled", 422, "Expressions not enabled")
PRECONDITION_FAILED = ProblemType("precondition-failed", 422, "Precondition failed")
ALREADY_EXISTS = ProblemType("already-exists", 409, "Already exists")
IDEMPOTENCY_CONFLICT = ProblemType("idempotency-conflict", 409, "Idempotency key reused")
LIMIT_EXCEEDED = ProblemType("limit-exceeded", 409, "Limit exceeded")


@dataclass(frozen=True)
class Finding:
    """A validation finding as the API reports it: a core `Issue` or a document problem.

    `path` locates it in the request (schema document paths use api names).
    """

    code: str
    message: str
    path: tuple[str | int, ...] = ()
    severity: str = "error"
    kind: str | None = None
    relation: str | None = None
    key: str | None = None
    attribute: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def of(cls, issue: Issue) -> Finding:
        return cls(
            code=issue.code.value,
            message=issue.message,
            path=tuple(issue.path),
            severity=issue.severity.value,
            kind=issue.kind,
            relation=issue.relation,
            key=issue.key,
            attribute=issue.attribute,
            details=dict(issue.details),
        )

    def document(self) -> dict[str, Any]:
        """JSON form; absent references are omitted."""
        body: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "path": list(self.path),
            "message": self.message,
        }
        for name in ("kind", "relation", "key", "attribute"):
            value = getattr(self, name)
            if value is not None:
                body[name] = value
        if self.details:
            body["details"] = self.details
        return body


def findings_of(issues: Iterable[Issue]) -> list[Finding]:
    return [Finding.of(issue) for issue in issues]


def finding_documents(findings: Iterable[Finding], limit: int) -> list[dict[str, Any]]:
    """JSON form of the first `limit` findings."""
    return [finding.document() for _, finding in zip(range(limit), findings, strict=False)]


def violations_problem(
    findings: Sequence[Finding], *, limit: int, extensions: dict[str, Any] | None = None
) -> ProblemError:
    """422 listing validation findings."""
    return ProblemError(
        VALIDATION_FAILED,
        f"{len(findings)} validation issue(s); nothing was written.",
        extensions={
            "issues": finding_documents(findings, limit),
            "issue_count": len(findings),
            **(extensions or {}),
        },
    )


class ProtectedBranchError(Exception):
    """A direct write to a protected branch; it must go through a Proposal."""

    def __init__(self, branch: str) -> None:
        super().__init__(f"branch {branch!r} is protected")
        self.branch = branch


def protected_branch_problem(domain_id: str, branch: str, proposals_path: str) -> ProblemError:
    """409 for direct writes to a protected branch."""
    return ProblemError(
        PROTECTED_BRANCH,
        f"Branch {branch!r} is protected: changes reach it through a Proposal. Commit to "
        "another branch and open a Proposal.",
        extensions={"domain": domain_id, "branch": branch, "proposals": proposals_path},
    )


def commit_problem(
    error: CommitError,
    *,
    max_issues: int,
    retry_after_s: int,
    locate: Callable[[Sequence[Issue]], Sequence[Finding]] = findings_of,
) -> ProblemError:
    """The problem detail answering a commit failure.

    `locate`, when given, rewrites validation issues (for example into YAML paths).
    """
    if isinstance(error, StaleHeadError):
        return ProblemError(
            STALE_HEAD,
            f"Branch {error.branch!r} moved; read the current head and retry.",
            extensions={
                "branch": error.branch,
                "expected_head": hex(error.expected),
                "current_head": None if error.actual is None else hex(error.actual),
            },
        )
    if isinstance(error, BranchLockTimeoutError):
        return ProblemError(
            BRANCH_BUSY,
            "Another writer holds the branch; retry later.",
            headers={"Retry-After": str(retry_after_s)},
        )
    if isinstance(error, ValidationFailedError):
        return violations_problem(locate(error.issues), limit=max_issues)
    if isinstance(error, SchemaChangeError):
        return ProblemError(SCHEMA_CHANGE_INVALID, str(error))
    if isinstance(error, BranchNotFoundError):
        return ProblemError(NOT_FOUND, str(error))
    if isinstance(error, BranchNotMaterializedError):
        return ProblemError(BRANCH_NOT_MATERIALIZED, str(error))
    raise error
