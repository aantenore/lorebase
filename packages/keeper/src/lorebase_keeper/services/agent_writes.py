"""Agent writes: Operations that need review become a task branch and a Proposal.

An Operation executed on a protected branch, or whose review policy asks for review, is
refused with `review-required` (see `services.operations`). A caller that names a `task`
gets this instead:

    review-required (protected branch or review policy)
    -> branch `{branch_prefix}/{principal}/{task}` (created from the target head as
       `branch_mode`, an overlay by default; reused when it exists and belongs to the caller)
    -> dry run (validation: violations are refused as usual, nothing is created)
    -> the Operation on the task branch (`reviewed_by_proposal`: the Proposal is the review)
    -> a Proposal task branch -> target, created or updated (one open Proposal per branch and
       target), titled from `title_template`, described by the `intent` and the executions
    -> merge, when the request asks for it and the Domain allows it for the caller's role
       (`allowed_roles_for_auto_merge`), the Proposal has no open Conflict and the approvals
       the target requires are in place (an Operation whose own review policy demands review
       also needs an actual approval).

The result is the Proposal and where it stands: `proposed`, `merged` or `conflicts`, with
`next` hints (problem-details style: action, detail, method, href) saying what comes next.
Every step has its own transaction and is repeatable: a retry with the same idempotency key
finds the commit it made, the branch and the Proposal. Without a key a retry runs the
Operation again on the task branch.

Settings are `keeper.agent_writes`; a Domain overrides any member with the same name in its
`agent_writes` setting. When a Proposal merges or closes, `release_branch` deletes the task
branch unless another open Proposal still uses it.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from lorebase_core.canonical import hex
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.rbac import AccessContext
from lorebase_keeper.context import StoreAccess, require_branch, require_domain
from lorebase_keeper.errors import VALIDATION_FAILED, ProblemError, ProblemType
from lorebase_keeper.services import branches, proposals
from lorebase_keeper.services import operations as operations_service
from lorebase_keeper.services.config import AgentWriteSettings
from lorebase_keeper.services.errors import (
    ALREADY_EXISTS,
    IDEMPOTENCY_CONFLICT,
    REVIEW_REQUIRED,
)
from lorebase_keeper.services.operations import (
    REVIEW_POLICY,
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionStatus,
)
from lorebase_keeper.settings import KeeperSettings
from lorebase_store.commit import StaleHeadError
from lorebase_store.materialize import BranchMode
from lorebase_store.models.enums import ProposalOrigin, ProposalStatus, RefKind
from lorebase_store.repositories import (
    InvocationRepository,
    InvocationRow,
    ProposalFilter,
    ProposalRepository,
    ProposalRow,
    RefRepository,
    RefRow,
)

logger = logging.getLogger("lorebase.keeper.agent_writes")

OVERRIDE_KEY = "agent_writes"
"""Key of the Domain setting that overrides `keeper.agent_writes`."""
DIVERTED = frozenset({REVIEW_REQUIRED, IDEMPOTENCY_CONFLICT})
"""Refusals of an execution on the target branch that the agent flow takes over: review is
needed, or the idempotency key was used by the earlier execution on the task branch."""
INTENT_MAX_CHARS = 4000
OPERATIONS_HEADING = "Operations:"
BRANCH_LIMIT = ProblemType("agent-branch-limit", 409, "Too many task branches")

Status = Literal["proposed", "merged", "conflicts"]
Hint = dict[str, str]
Check = Callable[[Permission, AccessContext], Awaitable[None]]
Roles = Callable[[AccessContext], Awaitable[frozenset[str]]]
Emit = Callable[..., Awaitable[None]]
"""`emit(action, target, **details)` writes an audit entry for a step of the flow."""

_UNSAFE = re.compile(r"[^a-z0-9._-]+|-{2,}")


# Pure helpers ----------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentRequest:
    """What the caller said about the review flow."""

    task: str | None = None
    intent: str | None = None
    auto_propose: bool | None = None
    auto_merge: bool | None = None


@dataclass(frozen=True)
class Decision:
    """What to do with an execution that needs review."""

    propose: bool
    merge: bool
    merge_requested: bool
    merge_allowed: bool
    refusal: str | None = None
    """Why no Proposal is opened: `disabled`, `declined` or `task-required`."""


def slug(text: str) -> str:
    """Lower case, runs of characters outside `a-z0-9._-` (and of dashes) become one `-`, no
    leading or trailing separators: usable as one segment of a branch name."""
    return _UNSAFE.sub("-", text.strip().lower()).strip("-._")


def task_slug(task: str, settings: AgentWriteSettings) -> str:
    """The normalized task; 422 when nothing is left or it is too long."""
    value = slug(task)
    if not value or len(value) > settings.max_task_length:
        raise ProblemError(
            VALIDATION_FAILED,
            "One or more request members are invalid.",
            extensions={
                "errors": [
                    {
                        "pointer": "/body/task",
                        "message": "must hold letters or digits and normalize to at most "
                        f"{settings.max_task_length} characters",
                        "code": "invalid",
                    }
                ]
            },
        )
    return value


def owner_prefix(settings: AgentWriteSettings, principal_id: str) -> str:
    """Start of the names of a principal's task branches."""
    owner = slug(principal_id)
    if not owner:
        raise ProblemError(VALIDATION_FAILED, "The principal id cannot form a branch name.")
    return f"{settings.branch_prefix}/{owner}/"


def branch_name(settings: AgentWriteSettings, principal_id: str, task: str) -> str:
    """`{branch_prefix}/{principal}/{task}`."""
    return owner_prefix(settings, principal_id) + task_slug(task, settings)


def is_task_branch(settings: AgentWriteSettings, name: str) -> bool:
    """Whether `name` has the shape of a task branch: `{branch_prefix}/{principal}/{task}`."""
    head = settings.branch_prefix + "/"
    return name.startswith(head) and name.removeprefix(head).count("/") == 1


def effective_settings(
    base: AgentWriteSettings, domain_settings: Mapping[str, Any] | None
) -> AgentWriteSettings:
    """`base` with the members the Domain's `agent_writes` setting overrides; 422 when that
    setting is not valid."""
    raw = (domain_settings or {}).get(OVERRIDE_KEY)
    if raw is None:
        return base
    try:
        if not isinstance(raw, Mapping):
            raise ValueError("must be an object")
        return AgentWriteSettings.model_validate({**base.model_dump(mode="python"), **raw})
    except (ValidationError, ValueError) as error:
        detail = error.errors()[0]["msg"] if isinstance(error, ValidationError) else str(error)
        raise ProblemError(
            VALIDATION_FAILED, f"The Domain setting {OVERRIDE_KEY} is not valid: {detail}"
        ) from None


def decide(
    settings: AgentWriteSettings,
    request: AgentRequest,
    *,
    agent: bool,
    roles: Collection[str],
) -> Decision:
    """Whether to open a Proposal, and whether to merge it at once.

    A Proposal needs the flow enabled, a task, and either `auto_propose` in the request or
    (when it leaves it out) an agent principal and `auto_propose` in the settings. Merging
    needs `auto_merge` (request, else settings) and one of `roles` in
    `allowed_roles_for_auto_merge`.
    """
    wanted = (
        request.auto_propose
        if request.auto_propose is not None
        else (settings.auto_propose and agent)
    )
    merge_requested = request.auto_merge if request.auto_merge is not None else settings.auto_merge
    merge_allowed = not set(roles).isdisjoint(settings.allowed_roles_for_auto_merge)
    refusal: str | None = None
    if not settings.enabled:
        refusal = "disabled"
    elif not wanted:
        refusal = "declined"
    elif request.task is None:
        refusal = "task-required"
    propose = refusal is None
    return Decision(
        propose=propose,
        merge=propose and merge_requested and merge_allowed,
        merge_requested=merge_requested,
        merge_allowed=merge_allowed,
        refusal=refusal,
    )


def merge_blockers(approval: proposals.Approval, *, policy_review: bool) -> list[str]:
    """Why a Proposal must not be merged automatically although the role may: approvals the
    target requires are missing, or the Operation's own review policy asks for a review and
    nobody approved."""
    blockers: list[str] = []
    if not approval.satisfied:
        blockers.append("approvals-pending")
    if policy_review and not approval.approvers:
        blockers.append("review-policy-needs-approval")
    return blockers


def operation_line(
    operation: str,
    parameters: Mapping[str, Any],
    invocation_id: str,
    settings: AgentWriteSettings,
) -> str:
    """One execution as listed in a Proposal description."""
    shown: list[str] = []
    for name in sorted(parameters):
        if not settings.summary_parameter_values:
            shown.append(name)
            continue
        value = json.dumps(parameters[name], ensure_ascii=False, default=str)
        if len(value) > settings.summary_value_chars:
            value = value[: settings.summary_value_chars - 1] + "…"
        shown.append(f"{name}={value}")
    return f"- {operation}({', '.join(shown)}) [{invocation_id}]"


def describe(
    intent: str | None, line: str, previous: str | None, settings: AgentWriteSettings
) -> str:
    """Description of a Proposal: the intent, then the executions (the earlier ones of
    `previous` first, at most `max_summary_operations` of the latest)."""
    heading = "\n\n" + OPERATIONS_HEADING + "\n"
    earlier_intent, _, tail = (previous or "").partition(heading)
    lines = [entry for entry in tail.splitlines() if entry.startswith("- ")]
    if line.rsplit("[", 1)[-1] not in {entry.rsplit("[", 1)[-1] for entry in lines}:
        lines.append(line)
    kept = lines[-settings.max_summary_operations :]
    text = (intent if intent is not None else earlier_intent).strip()
    listing = OPERATIONS_HEADING + "\n" + "\n".join(kept)
    return f"{text}\n\n{listing}" if text else listing


def hint(
    action: str,
    detail: str,
    *,
    method: str | None = None,
    href: str | None = None,
    permission: str | None = None,
) -> Hint:
    """A next step for the agent."""
    found = {"action": action, "detail": detail}
    for name, value in (("method", method), ("href", href), ("permission", permission)):
        if value is not None:
            found[name] = value
    return found


# Results ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposalSummary:
    id: str
    title: str
    status: str
    source_ref: str
    target_ref: str
    url: str
    created: bool


@dataclass(frozen=True)
class ApprovalSummary:
    required: int
    approvals: int
    satisfied: bool


@dataclass(frozen=True)
class AttemptSummary:
    id: str
    result: str | None
    open_conflicts: int
    resolved_conflicts: int
    counts: dict[str, dict[str, int]]


@dataclass(frozen=True)
class MergeSummary:
    requested: bool
    allowed: bool
    performed: bool
    blockers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentWriteOutcome:
    """Where an agent write stands."""

    status: Status
    operation: str
    branch: str
    target: str
    invocation_id: str
    parameters_hash: str
    revision: str | None
    revisions: list[str]
    edit_count: int
    replayed: bool
    proposal: ProposalSummary
    approval: ApprovalSummary
    attempt: AttemptSummary | None
    merge: MergeSummary
    merged_revision: str | None
    branch_created: bool
    branch_deleted: bool
    warnings: list[dict[str, Any]]
    next: list[Hint]


# Flow ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Prepared:
    settings: AgentWriteSettings
    default_branch: str
    target: RefRow
    branches: dict[str, RefRow]
    invocation: InvocationRow | None


def diverts(problem: ProblemError) -> bool:
    """Whether the agent flow may take over from this refusal."""
    return problem.problem in DIVERTED


def _paths(keeper: KeeperSettings, domain_id: str) -> str:
    return f"{keeper.api_prefix}/domains/{domain_id}"


def manual_hints(base: str, operation: str) -> list[Hint]:
    """Doing by hand what the flow does: a work branch, the Operation on it, a Proposal."""
    return [
        hint(
            "create_branch",
            "Create a work branch from the target and run the Operation there.",
            method="POST",
            href=f"{base}/branches",
            permission=Permission.BRANCH_CREATE.value,
        ),
        hint(
            "execute_on_branch",
            "Execute the Operation with `branch` set to the work branch.",
            method="POST",
            href=f"{base}/operations/{operation}/execute",
        ),
        hint(
            "open_proposal",
            "Open a Proposal from the work branch into the target.",
            method="POST",
            href=f"{base}/proposals",
            permission=Permission.PROPOSAL_CREATE.value,
        ),
    ]


def hinted(original: ProblemError, base: str, refusal: str | None) -> ProblemError:
    """`original` with `next` hints when it is `review-required`; other problems unchanged."""
    if original.problem is not REVIEW_REQUIRED:
        return original
    operation = str(original.extensions.get("operation", "{operation}"))
    steps: list[Hint] = []
    if refusal == "disabled":
        steps.append(hint("agent_writes_disabled", "Task branches are turned off for this Domain."))
    elif refusal == "declined":
        steps.append(
            hint("auto_propose", "Send `auto_propose: true` and a `task` to open a Proposal.")
        )
    else:
        steps.append(
            hint(
                "name_a_task",
                "Repeat the request with `task` (a short slug) and `intent`: Keeper runs the "
                "Operation on a task branch and opens a Proposal.",
                method="POST",
                href=f"{base}/operations/{operation}/execute",
            )
        )
    steps.extend(manual_hints(base, operation))
    original.extensions["next"] = steps
    return original


def next_steps(
    status: Status,
    base: str,
    proposal: ProposalSummary,
    *,
    approval: ApprovalSummary,
    merge: MergeSummary,
    merged_revision: str | None,
) -> list[Hint]:
    """What the agent (or a human on its behalf) does next."""
    here = f"{base}/proposals/{proposal.id}"
    if status == "merged":
        return [
            hint(
                "refresh",
                f"The target moved to {merged_revision}; take a new context pack before the "
                "next write.",
                method="POST",
                href=f"{base}/context",
            )
        ]
    if status == "conflicts":
        return [
            hint(
                "inspect_conflicts",
                "The Proposal has open Conflicts with the target.",
                method="GET",
                href=f"{here}/conflicts",
            ),
            hint(
                "resolve_conflicts",
                "A reviewer resolves them (choose ours, theirs or a value) and merges again.",
                method="POST",
                href=f"{here}/conflicts:resolve",
                permission=Permission.CONFLICT_RESOLVE.value,
            ),
            hint(
                "close_and_retry",
                "Or close the Proposal and run the Operation again on the current target.",
                method="POST",
                href=f"{here}/close",
            ),
        ]
    steps: list[Hint] = []
    if "approvals-pending" in merge.blockers or "review-policy-needs-approval" in merge.blockers:
        steps.append(
            hint(
                "await_review",
                f"Waiting for approval ({approval.approvals} of {approval.required} required).",
                method="POST",
                href=f"{here}/reviews",
                permission=Permission.PROPOSAL_REVIEW.value,
            )
        )
    elif not merge.performed:
        steps.append(
            hint(
                "merge",
                "Merging needs a maintainer"
                + ("" if merge.requested else " (auto_merge was not requested)")
                + (
                    ""
                    if merge.allowed or not merge.requested
                    else " (the role of this agent may not merge automatically)"
                )
                + ".",
                method="POST",
                href=f"{here}/merge",
                permission=Permission.PROPOSAL_MERGE.value,
            )
        )
    steps.append(hint("get_proposal", "Read the state of the Proposal.", method="GET", href=here))
    return steps


def _attempt_summary(view: proposals.AttemptView) -> AttemptSummary:
    per_code: dict[str, dict[str, int]] = {}
    for (code, resolved), count in sorted(view.counts.items()):
        entry = per_code.setdefault(code, {"open": 0, "resolved": 0})
        entry["resolved" if resolved else "open"] += count
    return AttemptSummary(
        id=view.attempt.id,
        result=view.attempt.result,
        open_conflicts=sum(c["open"] for c in per_code.values()),
        resolved_conflicts=sum(c["resolved"] for c in per_code.values()),
        counts=per_code,
    )


async def _prepare(
    store: StoreAccess,
    domain_id: str,
    request: ExecutionRequest,
    principal: Principal,
    keeper: KeeperSettings,
) -> _Prepared:
    async with store.sessions() as session, session.begin():
        domain = await require_domain(session, domain_id)
        target = await require_branch(session, domain_id, request.branch or domain.default_branch)
        refs = await RefRepository(session, domain_id).all()
        invocation = None
        if request.idempotency_key is not None:
            invocation = await InvocationRepository(session, domain_id).by_key(
                principal.id, request.idempotency_key
            )
        return _Prepared(
            settings=effective_settings(keeper.agent_writes, domain.settings),
            default_branch=domain.default_branch,
            target=target,
            branches={ref.name: ref for ref in refs},
            invocation=invocation,
        )


def _check_ownership(
    prepared: _Prepared, settings: AgentWriteSettings, principal: Principal, name: str
) -> None:
    existing = prepared.branches.get(name)
    if existing is not None:
        if existing.kind is not RefKind.BRANCH or existing.created_by != principal.id:
            raise ProblemError(
                ALREADY_EXISTS,
                f"A branch or tag named {name!r} exists and is not a task branch of this "
                "principal; use another task.",
            )
        return
    mine = sorted(
        n for n in prepared.branches if n.startswith(owner_prefix(settings, principal.id))
    )
    if len(mine) >= settings.max_open_branches_per_principal:
        raise ProblemError(
            BRANCH_LIMIT,
            f"{len(mine)} task branches are open (limit "
            f"{settings.max_open_branches_per_principal}); merge or close their Proposals "
            "first, or reuse one of them as the task.",
            extensions={"branches": mine},
        )


def _check_replay(
    prepared: _Prepared, request: ExecutionRequest, name: str
) -> InvocationRow | None:
    """The earlier execution under the idempotency key when it is this one; conflict when the
    key was used for something else."""
    found = prepared.invocation
    if found is None:
        return None
    digest = operations_service.parameters_hash(request.operation, name, request.parameters)
    if found.params.get("parameters_hash") != digest:
        raise ProblemError(
            IDEMPOTENCY_CONFLICT,
            "This Idempotency-Key was used with another Operation, branch or parameters.",
            extensions={"invocation_id": found.id},
        )
    return found


async def _find_proposal(
    store: StoreAccess, domain_id: str, source: str, target: str, *, any_status: bool
) -> ProposalRow | None:
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        found = await ProposalRepository(session, domain_id).page(
            ProposalFilter(
                status=None if any_status else ProposalStatus.OPEN,
                source_ref=source,
                target_ref=target,
            ),
            limit=1,
        )
    return found[0] if found else None


async def _upsert_proposal(
    store: StoreAccess,
    domain_id: str,
    keeper: KeeperSettings,
    settings: AgentWriteSettings,
    principal: Principal,
    *,
    name: str,
    target: str,
    task: str,
    intent: str | None,
    line: str,
    replayed: bool,
    emit: Emit,
) -> tuple[ProposalRow, bool]:
    """The Proposal of the task branch: created, or its description extended. A replay reuses
    the latest Proposal of the branch whatever its state."""
    existing = await _find_proposal(store, domain_id, name, target, any_status=replayed)
    if existing is None:
        title = settings.title_template.format(principal=principal.id, task=task)
        try:
            created = await proposals.create(
                store,
                domain_id,
                proposals.NewProposal(
                    source_ref=name,
                    target_ref=target,
                    title=title,
                    description=describe(intent, line, None, settings),
                    origin=ProposalOrigin.OPERATION,
                ),
                principal,
                keeper.proposals,
                idempotency_key=None,
            )
        except ProblemError as error:
            if error.problem is not ALREADY_EXISTS:
                raise
            existing = await _find_proposal(store, domain_id, name, target, any_status=False)
            if existing is None:
                raise
        else:
            row = created.view.proposal
            await emit(
                "proposal.create",
                row.id,
                source=name,
                target_branch=target,
                origin=ProposalOrigin.OPERATION.value,
            )
            return row, True
    if existing.status is ProposalStatus.OPEN and existing.author == principal.id:
        text = describe(intent, line, existing.description, settings)
        if text != existing.description:
            view = await proposals.update(
                store, domain_id, existing.id, keeper.proposals, title=None, description=text
            )
            await emit("proposal.update", existing.id, fields=["description"])
            return view.proposal, False
    return existing, False


async def run(
    store: StoreAccess,
    domain_id: str,
    request: ExecutionRequest,
    agent: AgentRequest,
    principal: Principal,
    keeper: KeeperSettings,
    *,
    original: ProblemError,
    check: Check,
    roles_of: Roles,
    emit: Emit,
) -> ExecutionOutcome | AgentWriteOutcome:
    """Run `request` through a task branch and a Proposal after `original` refused it.

    Raises `original` (with `next` hints) when the flow does not apply: no task, turned off,
    or declined by the request. Violations, stale heads and permission failures are raised as
    for any execution, before anything is created. Returns an `ExecutionOutcome` for an
    Operation that has nothing to change (no branch or Proposal is made for it).
    """
    base = _paths(keeper, domain_id)
    if agent.task is None:
        raise hinted(original, base, "task-required")
    prepared = await _prepare(store, domain_id, request, principal, keeper)
    settings = prepared.settings
    name = branch_name(settings, principal.id, agent.task)
    task = name.rsplit("/", 1)[1]
    branches.check_ref_name(name, keeper.branches, "/body/task")
    target = prepared.target.name
    context = AccessContext(branch=name)
    decision = decide(
        settings,
        agent,
        agent=principal.type in settings.principal_types,
        roles=await roles_of(context),
    )
    if not decision.propose:
        raise hinted(original, base, decision.refusal)
    reason = str(original.extensions.get("reason", ""))

    _check_ownership(prepared, settings, principal, name)
    exists = name in prepared.branches
    replay = _check_replay(prepared, request, name)
    await check(Permission.DATA_READ, AccessContext(branch=target))
    await check(Permission.PROPOSAL_CREATE, context)
    if not exists:
        await check(Permission.BRANCH_CREATE, context)
    if request.expected_head is not None and request.expected_head != prepared.target.head:
        raise StaleHeadError(target, request.expected_head, prepared.target.head)

    on_branch = ExecutionRequest(
        operation=request.operation,
        parameters=request.parameters,
        branch=name,
        idempotency_key=request.idempotency_key,
        message=request.message,
        reviewed_by_proposal=True,
    )
    proposals_path = f"{base}/proposals"
    if replay is None:
        dry = await operations_service.execute(
            store,
            domain_id,
            ExecutionRequest(
                operation=request.operation,
                parameters=request.parameters,
                branch=name if exists else target,
                dry_run=True,
                message=request.message,
                reviewed_by_proposal=True,
            ),
            principal,
            keeper.operations,
            check=check,
            proposals_path=proposals_path,
        )
        if dry.violations:
            raise operations_service.refused(dry, limit=keeper.operations.max_issues)
        if dry.edit_count == 0 and not exists:
            dry.status = ExecutionStatus.COMMITTED
            dry.branch = target
            return dry

    created_branch = False
    if replay is None or exists:
        if not exists:
            try:
                await branches.create(
                    store,
                    domain_id,
                    name,
                    principal,
                    keeper.branches,
                    source=target,
                    revision=None,
                    idempotency_key=None,
                    mode=BranchMode(settings.branch_mode),
                )
                created_branch = True
                await emit("branch.create", name, source=target, purpose="agent-task")
            except ProblemError as error:
                if error.problem is not ALREADY_EXISTS:
                    raise
        executed = await operations_service.execute(
            store,
            domain_id,
            on_branch,
            principal,
            keeper.operations,
            check=check,
            proposals_path=proposals_path,
        )
    else:
        executed = _recalled(replay, name)

    parameters = (
        replay.params.get("parameters", request.parameters) if replay else (request.parameters)
    )
    line = operation_line(request.operation, parameters, executed.invocation_id, settings)
    row, created = await _upsert_proposal(
        store,
        domain_id,
        keeper,
        settings,
        principal,
        name=name,
        target=target,
        task=task,
        intent=agent.intent,
        line=line,
        replayed=replay is not None,
        emit=emit,
    )
    return await _settle(
        store,
        domain_id,
        keeper,
        settings,
        principal,
        decision,
        row,
        created,
        executed,
        emit=emit,
        branch_created=created_branch,
        policy_review=reason == REVIEW_POLICY,
    )


def _recalled(found: InvocationRow, branch: str) -> ExecutionOutcome:
    """The result of an execution whose task branch is gone (its Proposal was merged)."""
    params = found.params
    return ExecutionOutcome(
        status=ExecutionStatus.COMMITTED,
        operation=str(params.get("operation")),
        branch=branch,
        invocation_id=found.id,
        parameters_hash=str(params.get("parameters_hash")),
        head=bytes.fromhex(str(params["head"])),
        revisions=[bytes.fromhex(str(r)) for r in params.get("revisions", [])],
        edit_count=int(params.get("edit_count", 0)),
        replayed=True,
    )


async def _settle(
    store: StoreAccess,
    domain_id: str,
    keeper: KeeperSettings,
    settings: AgentWriteSettings,
    principal: Principal,
    decision: Decision,
    row: ProposalRow,
    created: bool,
    executed: ExecutionOutcome,
    *,
    emit: Emit,
    branch_created: bool,
    policy_review: bool,
) -> AgentWriteOutcome:
    """Conflicts, approvals and the merge: the state the response reports."""
    base = _paths(keeper, domain_id)
    view = await proposals.get(store, domain_id, row.id, keeper.proposals)
    status: Status = "proposed"
    attempt: AttemptSummary | None = None
    blockers: list[str] = []
    performed = False
    deleted = False
    merged_revision: str | None = None
    current = view.proposal
    if current.status is ProposalStatus.MERGED:
        status = "merged"
        merged_revision = None if current.merged_revision is None else hex(current.merged_revision)
    elif current.status is ProposalStatus.CLOSED:
        raise ProblemError(
            proposals.PROPOSAL_NOT_OPEN,
            f"Proposal {current.id!r} was closed; use another task for a new Proposal.",
            extensions={"proposal": current.id, "status": current.status.value},
        )
    else:
        conflicts = await proposals.conflicts(
            store,
            domain_id,
            current.id,
            principal,
            keeper.proposals,
            proposals.ConflictQuery(limit=1),
        )
        attempt = _attempt_summary(conflicts)
        blockers = merge_blockers(view.approval, policy_review=policy_review)
        if attempt.open_conflicts:
            status = "conflicts"
        elif decision.merge and not blockers:
            merged = await _merge(store, domain_id, keeper, principal, current)
            if merged is None:
                blockers = ["approvals-pending"]
            elif merged.head is None:
                status, attempt = "conflicts", _attempt_summary(merged.view)
            else:
                performed, status = True, "merged"
                current = merged.proposal
                merged_revision = hex(merged.head)
                attempt = _attempt_summary(merged.view)
                await emit(
                    "proposal.merge",
                    current.id,
                    revision=merged_revision,
                    result=merged.view.attempt.result,
                    automatic=True,
                )
                deleted = merged.source_deleted or await release_branch(
                    store, domain_id, current, keeper, emit=emit
                )
        elif decision.merge:
            logger.info("automatic merge held back: %s", ", ".join(blockers))
    summary = ProposalSummary(
        id=current.id,
        title=current.title,
        status=current.status.value,
        source_ref=current.source_ref,
        target_ref=current.target_ref,
        url=f"{base}/proposals/{current.id}",
        created=created,
    )
    approval = ApprovalSummary(
        required=view.approval.required,
        approvals=len(view.approval.approvers),
        satisfied=view.approval.satisfied,
    )
    merge = MergeSummary(
        requested=decision.merge_requested,
        allowed=decision.merge_allowed,
        performed=performed,
        blockers=blockers,
    )
    revisions = [hex(r) for r in executed.revisions]
    return AgentWriteOutcome(
        status=status,
        operation=executed.operation,
        branch=current.source_ref,
        target=current.target_ref,
        invocation_id=executed.invocation_id,
        parameters_hash=executed.parameters_hash,
        revision=revisions[-1] if revisions else None,
        revisions=revisions,
        edit_count=executed.edit_count,
        replayed=executed.replayed,
        proposal=summary,
        approval=approval,
        attempt=attempt,
        merge=merge,
        merged_revision=merged_revision,
        branch_created=branch_created,
        branch_deleted=deleted,
        warnings=[f.document() for f in executed.warnings],
        next=next_steps(
            status,
            base,
            summary,
            approval=approval,
            merge=merge,
            merged_revision=merged_revision,
        ),
    )


async def _merge(
    store: StoreAccess,
    domain_id: str,
    keeper: KeeperSettings,
    principal: Principal,
    row: ProposalRow,
) -> proposals.MergeOutcome | None:
    """Merge `row`: the outcome (`head` is `None` while Conflicts remain), or `None` when the
    approvals went missing meanwhile."""
    try:
        outcome = await proposals.merge(
            store,
            domain_id,
            row.id,
            principal,
            keeper.proposals,
            idempotency_key=None,
            message=f"Merge {row.source_ref} (automatic)",
            page_limit=1,
        )
    except ProblemError as error:
        if error.problem is proposals.APPROVALS_REQUIRED:
            return None
        raise
    return outcome


# Cleanup ---------------------------------------------------------------------------------


async def release_branch(
    store: StoreAccess,
    domain_id: str,
    proposal: ProposalRow,
    keeper: KeeperSettings,
    *,
    emit: Emit | None = None,
) -> bool:
    """Delete the task branch of a Proposal that merged or closed, unless
    `delete_branch_on_merge` is off or another open Proposal still has it as source. Never
    raises for a branch that cannot be deleted; returns whether it was."""
    if proposal.status is ProposalStatus.OPEN:
        return False
    async with store.sessions() as session, session.begin():
        domain = await require_domain(session, domain_id)
        settings = effective_settings(keeper.agent_writes, domain.settings)
        source = proposal.source_ref
        if not (settings.delete_branch_on_merge and is_task_branch(settings, source)):
            return False
        ref = await RefRepository(session, domain_id).get(source)
        if ref is None or ref.kind is not RefKind.BRANCH or ref.protected:
            return False
        if source == domain.default_branch:
            return False
        others = await ProposalRepository(session, domain_id).page(
            ProposalFilter(status=ProposalStatus.OPEN, source_ref=source), limit=1
        )
        if others:
            return False
    try:
        await branches.delete(store, domain_id, source, keeper.branches)
    except ProblemError as error:
        logger.warning("task branch %s kept: %s", source, error.problem_detail)
        return False
    if emit is not None:
        await emit("branch.delete", source, reason=f"proposal-{proposal.status.value}")
    return True


def audit_details(outcome: AgentWriteOutcome) -> dict[str, Any]:
    """Facts about an agent write for the audit entry of the request."""
    return {
        "invocation_id": outcome.invocation_id,
        "parameters_hash": outcome.parameters_hash,
        "branch": outcome.branch,
        "target_branch": outcome.target,
        "proposal": outcome.proposal.id,
        "status": outcome.status,
        "revisions": outcome.revisions,
        "replayed": outcome.replayed,
        "merge_requested": outcome.merge.requested,
        "merge_performed": outcome.merge.performed,
    }
