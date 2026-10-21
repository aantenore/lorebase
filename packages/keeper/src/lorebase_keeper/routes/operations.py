"""Operation execution (design 5.7, 10.1, 11.1): `POST /domains/{d}/operations/{op}/execute`.

Built-in Operations need `operation.execute` (role `editor`), stored ones
`operation.execute_custom` (role `operator`) and, when the Operation lists `permissions`, one
of the principals or groups named there. Grants narrowed to a Kind, Operation or branch
pattern apply. The response carries `status` (`committed` or `dry_run`), the computed `edits`,
`violations` and the new Revision(s); rejected executions are 422 problems with the same
members (`precondition-failed` when a CEL precondition does not hold), and executions that
need review (on a protected branch, or when a review policy expression is true) are 409
problems pointing to Proposals.

An agent that names a `task` gets a Proposal instead of that 409 (`services.agent_writes`): the
Operation runs on the branch `agent/{principal}/{task}` and the answer is 202 with the
Proposal and where it stands (`proposed`, `merged` or `conflicts`), `next` hints included.
`intent` goes into the Proposal description; `auto_propose` and `auto_merge` override the
defaults of `keeper.agent_writes` (merging only if the Domain allows it for the caller's role).
Requests of this kind are `writes` for rate limiting like any other POST.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
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
from lorebase_keeper.services import agent_writes
from lorebase_keeper.services import operations as service
from lorebase_keeper.services.errors import BRANCH_BUSY, commit_problem, finding_documents
from lorebase_store.auth import NewAuditEntry
from lorebase_store.commit import CommitError
from lorebase_store.repositories import LockTimeoutError

router = APIRouter(tags=["operations"])


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
    task: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Short slug naming the piece of work. When the Operation needs review "
        "(protected branch, review policy), Keeper runs it on the branch "
        "`agent/{principal}/{task}` and opens or updates a Proposal; without a task the "
        "refusal is the 409 `review-required`. Ignored when the Operation commits directly.",
    )
    intent: str | None = Field(
        default=None,
        max_length=agent_writes.INTENT_MAX_CHARS,
        description="What the agent is after, in a sentence or two; becomes the start of the "
        "Proposal description.",
    )
    auto_propose: bool | None = Field(
        default=None,
        description="Open a Proposal when review is needed. Default: "
        "`keeper.agent_writes.auto_propose` for agent principals, false for others. `true` "
        "needs a `task`.",
    )
    auto_merge: bool | None = Field(
        default=None,
        description="Merge the Proposal at once. Default: `keeper.agent_writes.auto_merge`. "
        "Honoured only when the Domain allows it for the caller's role, there is no open "
        "Conflict and the required approvals are in.",
    )


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


class ProposalRefOut(BaseModel):
    """The Proposal carrying the edits of an agent write."""

    id: str
    title: str
    status: str
    source_ref: str
    target_ref: str
    url: str
    created: bool = Field(description="False when an open Proposal of the branch was updated.")


class AgentApprovalOut(BaseModel):
    required: int
    approvals: int
    satisfied: bool


class AgentAttemptOut(BaseModel):
    """The merge attempt: Conflicts between the task branch and the target."""

    id: str
    result: str | None
    open_conflicts: int
    resolved_conflicts: int
    counts: dict[str, dict[str, int]]


class AgentMergeOut(BaseModel):
    requested: bool
    allowed: bool = Field(description="The Domain allows automatic merges for the caller's role.")
    performed: bool
    blockers: list[str] = Field(
        description="What keeps the Proposal from merging by itself: `approvals-pending` (the "
        "target needs approvals), `review-policy-needs-approval` (the Operation's review "
        "policy asks for a review and nobody approved)."
    )


class NextHint(BaseModel):
    """A next step, in the style of problem details extensions."""

    action: str
    detail: str
    method: str | None = None
    href: str | None = None
    permission: str | None = None


class AgentWriteOut(BaseModel):
    """An Operation that needed review, run on a task branch and proposed."""

    status: Literal["proposed", "merged", "conflicts"]
    operation: str
    branch: str = Field(description="The task branch.")
    target: str = Field(description="The branch the Proposal merges into.")
    invocation_id: str
    parameters_hash: str
    revision: str | None = Field(description="Last Revision written on the task branch.")
    revisions: list[str]
    edit_count: int
    replayed: bool
    proposal: ProposalRefOut
    approval: AgentApprovalOut
    attempt: AgentAttemptOut | None
    merge: AgentMergeOut
    merged_revision: str | None = Field(description="Head of the target after an automatic merge.")
    branch_created: bool
    branch_deleted: bool
    warnings: list[dict[str, Any]]
    next: list[NextHint]


_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks the permission named in the body."},
    404: {"model": Problem, "description": "Domain, branch or Operation not found."},
    202: {
        "model": AgentWriteOut,
        "description": "The Operation needs review and ran on a task branch; a Proposal "
        "carries the edits (`status`: `proposed`, `merged` or `conflicts`).",
    },
    409: {
        "model": Problem,
        "description": "Review required (`review-required`, with `next` hints), head moved "
        "(`stale-head`), idempotency key reused with other parameters (`idempotency-conflict`) "
        "or too many task branches (`agent-branch-limit`).",
    },
    422: {
        "model": Problem,
        "description": "Invalid parameters or violations (`issues`, `edits`), a precondition "
        "that does not hold (`precondition-failed`, with the expression and its position), or "
        "expressions not enabled (`expressions-not-enabled`).",
    },
    503: {"model": Problem, "description": "The branch is locked by another writer."},
}


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
    response_model=ExecutionOut,
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
) -> ExecutionOut | JSONResponse:
    """Validate parameters, compute the edits and commit them as a Revision (or dry-run).

    An Operation that needs review is a 409 `review-required` unless the request names a
    `task`: then it runs on a task branch and the answer is 202 with the Proposal.
    """
    settings = keeper.operations
    if body.auto_propose and body.task is None:
        raise ProblemError(
            VALIDATION_FAILED,
            "One or more request members are invalid.",
            extensions={
                "errors": [
                    {
                        "pointer": "/body/task",
                        "message": "is required with auto_propose",
                        "code": "required",
                    }
                ]
            },
        )

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

    async def emit(action: str, target: str, **details: Any) -> None:
        if auth.audit_writer is not None:
            await auth.audit_writer.write(
                NewAuditEntry(
                    actor=principal.id,
                    action=action,
                    domain_id=domain_id,
                    target=target,
                    details={"outcome": "success", "via": "agent_write", **details},
                )
            )

    async def roles_of(context: AccessContext) -> frozenset[str]:
        return await auth.authorizer.roles_of(principal, domain_id, context)

    outcome: service.ExecutionOutcome | agent_writes.AgentWriteOutcome
    try:
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
        except ProblemError as refusal:
            if body.dry_run or not agent_writes.diverts(refusal):
                raise
            outcome = await agent_writes.run(
                store,
                domain_id,
                execution,
                agent_writes.AgentRequest(
                    task=body.task,
                    intent=body.intent,
                    auto_propose=body.auto_propose,
                    auto_merge=body.auto_merge,
                ),
                principal,
                keeper,
                original=refusal,
                check=check,
                roles_of=roles_of,
                emit=emit,
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
    if isinstance(outcome, agent_writes.AgentWriteOutcome):
        annotate(
            request,
            action="operation.propose",
            target=operation,
            task=body.task,
            **agent_writes.audit_details(outcome),
        )
        if outcome.replayed:
            response.headers[REPLAYED_HEADER] = "true"
        return JSONResponse(
            status_code=202,
            content=AgentWriteOut.model_validate(asdict(outcome)).model_dump(mode="json"),
            headers=dict(response.headers),
        )
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
