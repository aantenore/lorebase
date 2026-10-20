"""Operation execution (design 5.7, 10.1, 11.1): `POST /domains/{d}/operations/{op}/execute`.

Built-in Operations need `operation.execute` (role `editor`), stored ones
`operation.execute_custom` (role `operator`) and, when the Operation lists `permissions`, one
of the principals or groups named there. Grants narrowed to a Kind, Operation or branch
pattern apply. The response carries `status` (`committed` or `dry_run`), the computed `edits`,
`violations` and the new Revision(s); rejected executions are 422 problems with the same
members (`precondition-failed` when a CEL precondition does not hold), and executions that
need review (on a protected branch, or when a review policy expression is true) are 409
problems pointing to Proposals.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field, JsonValue

from lorebase_core.canonical import hex
from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import AuthDep, PrincipalDep, authorize
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.rbac import AccessContext
from lorebase_keeper.context import StoreDep, parse_revision_id
from lorebase_keeper.deps import IdempotencyKeyDep, KeeperSettingsDep, get_idempotency_key
from lorebase_keeper.errors import VALIDATION_FAILED, Problem, ProblemError
from lorebase_keeper.routes.domains import REPLAYED_HEADER
from lorebase_keeper.services import operations as service
from lorebase_keeper.services.errors import BRANCH_BUSY, commit_problem, finding_documents
from lorebase_store.commit import CommitError
from lorebase_store.repositories import LockTimeoutError

router = APIRouter(tags=["operations"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks the permission named in the body."},
    404: {"model": Problem, "description": "Domain, branch or Operation not found."},
    409: {
        "model": Problem,
        "description": "Review required (`review-required`), head moved (`stale-head`) or "
        "idempotency key reused with other parameters (`idempotency-conflict`).",
    },
    422: {
        "model": Problem,
        "description": "Invalid parameters or violations (`issues`, `edits`), a precondition "
        "that does not hold (`precondition-failed`, with the expression and its position), or "
        "expressions not enabled (`expressions-not-enabled`).",
    },
    503: {"model": Problem, "description": "The branch is locked by another writer."},
}


class ExecuteRequest(BaseModel):
    """An execution of an Operation."""

    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    branch: str | None = Field(default=None, description="Default: the default branch.")
    dry_run: bool = Field(
        default=False, description="Compute edits and violations without committing."
    )
    idempotency_key: str | None = Field(
        default=None,
        description="Same as the Idempotency-Key header; a retry with the same key and "
        "parameters returns the original result.",
    )
    expected_head: str | None = Field(
        default=None, description="Fail with 409 unless the branch head is this Revision."
    )
    message: str | None = Field(default=None, max_length=2000)


class ExecutionOut(BaseModel):
    """Result of an execution."""

    status: Literal["committed", "dry_run"]
    operation: str
    branch: str
    invocation_id: str
    parameters_hash: str
    head: str = Field(description="Branch head the Operation ran against.")
    revision: str | None = Field(description="Last Revision written; null for dry runs.")
    revisions: list[str] = Field(description="Every Revision written (imports run in batches).")
    edits: list[dict[str, Any]]
    edit_count: int
    edits_truncated: bool
    violations: list[dict[str, Any]]
    violation_count: int
    warnings: list[dict[str, Any]]
    review_required: bool = Field(
        description="True when a non-dry run would need a Proposal on this branch."
    )
    replayed: bool


def _key(header: str | None, body: str | None) -> str | None:
    if body is not None:
        get_idempotency_key(body)
    if header is not None and body is not None and header != body:
        raise ProblemError(
            VALIDATION_FAILED,
            "The Idempotency-Key header and idempotency_key differ.",
        )
    return header or body


@router.post(
    "/domains/{domain_id}/operations/{operation}/execute",
    summary="Execute an Operation",
    responses=_ERRORS,
)
async def execute_operation(
    domain_id: str,
    operation: str,
    body: ExecuteRequest,
    request: Request,
    response: Response,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    header_key: IdempotencyKeyDep,
) -> ExecutionOut:
    """Validate parameters, compute the edits and commit them as a Revision (or dry-run)."""
    settings = keeper.operations

    async def check(permission: Permission, context: AccessContext) -> None:
        await authorize(request, auth, principal, permission, domain_id, context)

    execution = service.ExecutionRequest(
        operation=operation,
        parameters=body.parameters,
        branch=body.branch,
        dry_run=body.dry_run,
        idempotency_key=_key(header_key, body.idempotency_key),
        expected_head=None
        if body.expected_head is None
        else parse_revision_id(body.expected_head, "/body/expected_head"),
        message=body.message,
    )
    try:
        outcome = await service.execute(
            store,
            domain_id,
            execution,
            principal,
            settings,
            check=check,
            proposals_path=f"{keeper.api_prefix}/domains/{domain_id}/proposals",
        )
    except LockTimeoutError:
        raise ProblemError(
            BRANCH_BUSY,
            "Another writer holds the branch; retry later.",
            headers={"Retry-After": str(keeper.branches.lock_retry_after_s)},
        ) from None
    except CommitError as error:
        raise commit_problem(
            error,
            max_issues=settings.max_issues,
            retry_after_s=keeper.branches.lock_retry_after_s,
        ) from None
    revisions = [hex(r) for r in outcome.revisions]
    annotate(
        request,
        action="operation.dry_run" if body.dry_run else "operation.execute",
        target=operation,
        branch=outcome.branch,
        invocation_id=outcome.invocation_id,
        parameters_hash=outcome.parameters_hash,
        revisions=revisions,
        replayed=outcome.replayed,
    )
    if outcome.replayed:
        response.headers[REPLAYED_HEADER] = "true"
    return ExecutionOut(
        status=outcome.status.value,
        operation=outcome.operation,
        branch=outcome.branch,
        invocation_id=outcome.invocation_id,
        parameters_hash=outcome.parameters_hash,
        head=hex(outcome.head),
        revision=revisions[-1] if revisions else None,
        revisions=revisions,
        edits=outcome.edits,
        edit_count=outcome.edit_count,
        edits_truncated=len(outcome.edits) < outcome.edit_count,
        violations=finding_documents(outcome.violations, settings.max_issues),
        violation_count=len(outcome.violations),
        warnings=finding_documents(outcome.warnings, settings.max_issues),
        review_required=outcome.review_required,
        replayed=outcome.replayed,
    )
