"""Proposals, reviews, merges and Conflict resolution (design 6.8 to 6.10, 10.3, 11.1).

| Method and path | Permission | Notes |
|-----------------|------------|-------|
| `POST .../proposals` | `proposal.create` (source), `data.read` (target) | 201; key replays 200 |
| `GET .../proposals` | `data.read` | filters `status`, `origin`, `source`, `target`, `author` |
| `GET .../proposals/{p}` | `data.read` (both branches) | reviews, approvals, latest attempt |
| `PATCH .../proposals/{p}` | author, else `proposal.merge` | title, description |
| `POST .../proposals/{p}/close` | author, else `proposal.merge` | attempts abandoned |
| `POST .../proposals/{p}/reviews` | `proposal.review` (target) | 201 |
| `POST .../proposals/{p}/merge` | `proposal.merge` (target), read | 200; 409 `merge-conflicts` |
| `GET .../proposals/{p}/conflicts` | `data.read` (both branches) | attempt, page of Conflicts |
| `POST .../proposals/{p}/conflicts/{c}/resolution` | `conflict.resolve` (target), read | stores |
| `POST .../proposals/{p}/conflicts:resolve` | `conflict.resolve` (target), read | stores, re-runs |

The merge answers 200 with the merge outcome when the target moved (merge Revision,
fast-forward or already up to date) and 409 `merge-conflicts` when Conflicts remain; that
problem carries the stored attempt (`attempt`, same shape as the Conflict listing, open
Conflicts first page). Conflicts are identified by their fingerprint. Reading the Conflicts of
an open Proposal recomputes its attempt when a head moved since (6.10, stale attempts).

Reads are authorized against the source and the target branch (`services.proposal_reads`):
listings keep the Proposals the caller may read, so a page may hold fewer items than asked
while `next_cursor` is set; callers limited to Kinds see the Conflicts of those Kinds only.
Writes keep that scope ("read" above): merging and resolving need the read access of the
Conflict listing, a resolution naming a Conflict outside it is refused (403), and the
attempts they return list only the Conflicts the caller may read. Proposal details count only
those Conflicts too, and the merge check of a caller who does not see every Conflict states no
count.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field, JsonValue

from lorebase_core.canonical import hex
from lorebase_core.merge import ConflictKind
from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import Access, AuthDep, PrincipalDep, authorize, require
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.rbac import AccessContext
from lorebase_keeper.context import StoreDep
from lorebase_keeper.deps import IdempotencyKeyDep, KeeperSettingsDep
from lorebase_keeper.errors import VALIDATION_FAILED, Problem, ProblemError, ValidationProblem
from lorebase_keeper.pagination import Page, PageParamsDep, encode_cursor
from lorebase_keeper.reading.points import read_guard
from lorebase_keeper.routes.domains import REPLAYED_HEADER
from lorebase_keeper.services import proposals as service
from lorebase_keeper.services.errors import commit_problem
from lorebase_keeper.services.proposal_reads import (
    ConflictScope,
    conflict_scope,
    readable_proposals,
    require_target_read,
    visible_scope,
)
from lorebase_keeper.services.reads import ReadGuard
from lorebase_store.commit import CommitError
from lorebase_store.models.enums import ProposalOrigin, ProposalStatus, ReviewVerdict
from lorebase_store.repositories import (
    ConflictRow,
    MergeAttemptRow,
    ProposalFilter,
    ProposalRow,
    ResolutionRow,
    ReviewRow,
)

router = APIRouter(tags=["proposals"])

PROPOSALS = "/domains/{domain_id}/proposals"
PROPOSAL = PROPOSALS + "/{proposal_id}"

DataRead = Annotated[ReadGuard, Depends(read_guard())]

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "Missing permission, or self-approval refused."},
    404: {"model": Problem, "description": "Domain, Proposal, branch or Conflict not found."},
    409: {
        "model": Problem,
        "description": "Proposal not open (`proposal-not-open`), approvals missing "
        "(`approvals-required`), Conflicts remain (`merge-conflicts`, with `attempt`), a "
        "branch is gone (`proposal-branch-missing`), duplicate or idempotency conflict.",
    },
    422: {"model": ValidationProblem, "description": "Invalid request or resolution."},
    503: {"model": Problem, "description": "The target branch is locked by another writer."},
}

_CODES = frozenset(kind.value for kind in ConflictKind)


# Models -----------------------------------------------------------------------------------


class ProposalCreate(BaseModel):
    """A new Proposal: merge `source_ref` into `target_ref`."""

    source_ref: str = Field(min_length=1, max_length=200)
    target_ref: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20000)


class ProposalUpdate(BaseModel):
    """Title and description; omitted members are left unchanged."""

    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20000)


class CheckOut(BaseModel):
    """One Proposal check (design 10.3)."""

    name: str
    status: Literal["passed", "failed", "pending"]
    message: str | None = None


class ProposalOut(BaseModel):
    id: str
    title: str
    description: str | None
    source_ref: str
    target_ref: str
    origin: ProposalOrigin
    status: ProposalStatus
    author: str
    created_at: datetime
    updated_at: datetime | None
    merged_revision: str | None
    policy: dict[str, Any] = Field(description="Merge policy options differing from defaults.")
    checks: list[CheckOut]


class ReviewIn(BaseModel):
    verdict: ReviewVerdict
    comment: str | None = Field(default=None, max_length=20000)


class ReviewOut(BaseModel):
    id: str
    reviewer: str
    verdict: ReviewVerdict
    comment: str | None
    at: datetime


class ApprovalOut(BaseModel):
    """Review state against the target branch's protection."""

    protected: bool
    required: int
    approvals: int
    approvers: list[str]
    changes_requested: list[str]
    satisfied: bool


class ResolutionIn(BaseModel):
    """A decision for one Conflict (`value` or per-member `values` for `custom` only)."""

    choice: str
    value: JsonValue = None
    values: dict[str, JsonValue] | None = None


class FingerprintedResolutionIn(ResolutionIn):
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ResolveManyIn(BaseModel):
    resolutions: list[FingerprintedResolutionIn] = Field(min_length=1)


class ResolutionOut(BaseModel):
    """A stored resolution; `matched` is false when its Conflict is gone from the attempt."""

    fingerprint: str
    choice: str
    value: JsonValue = None
    values: dict[str, JsonValue] | None = None
    resolver: str
    at: datetime
    matched: bool = True


class AttemptOut(BaseModel):
    """A merge attempt (design 6.8 step 5) and the resolutions of its Proposal."""

    id: str
    proposal: str
    status: str
    result: str | None = Field(description="`conflicts`, `merged`, `fast_forward` or `up_to_date`.")
    base: str | None
    ours: str
    theirs: str
    virtual_base: bool
    created_at: datetime
    created_by: str | None
    revision: str | None = Field(description="Target head after the merge committed.")
    policy: dict[str, Any]
    open_conflicts: int
    resolved_conflicts: int
    counts: dict[str, dict[str, int]] = Field(description="Per Conflict code: open, resolved.")
    recomputed: bool = Field(description="This request ran the merge.")
    stale: bool = Field(description="A head moved since the previous attempt.")
    resolutions: list[ResolutionOut]
    unmatched_resolutions: list[str]
    auto_resolutions: list[dict[str, Any]]


class ConflictOut(BaseModel):
    """A Conflict (core `Conflict`) with its id, state and members."""

    model_config = {"extra": "allow"}

    id: str
    status: Literal["open", "resolved"]
    resolution: dict[str, Any] | None = None


class ConflictsOut(AttemptOut):
    """An attempt with one page of its Conflicts."""

    conflicts: list[ConflictOut]
    next_cursor: str | None = None


class ProposalDetailOut(ProposalOut):
    reviews: list[ReviewOut]
    approval: ApprovalOut
    attempt: AttemptOut | None


class ReviewResult(BaseModel):
    review: ReviewOut
    approval: ApprovalOut


class MergeOut(BaseModel):
    """A merged Proposal."""

    status: Literal["merged"] = "merged"
    result: str = Field(description="`merged`, `fast_forward` or `up_to_date`.")
    revision: str = Field(description="Head of the target branch after the merge.")
    proposal: ProposalOut
    attempt: ConflictsOut
    source_deleted: bool
    replayed: bool


# Mapping ----------------------------------------------------------------------------------


def _checks(
    row: ProposalRow, approval: service.Approval | None = None, *, counted: bool = True
) -> list[CheckOut]:
    """The checks of `row`; `counted` false leaves the number of open Conflicts out (callers
    who do not see every Conflict)."""
    checks: list[CheckOut] = []
    merge = (row.checks or {}).get("merge")
    if isinstance(merge, dict):
        open_count = int(merge.get("open_conflicts") or 0)
        status: Literal["passed", "failed", "pending"] = "passed" if open_count == 0 else "failed"
        if open_count == 0:
            message = "No Conflicts."
        elif counted:
            message = f"{open_count} open Conflict(s) at {str(merge.get('ours'))[:12]}."
        else:
            message = f"Open Conflicts at {str(merge.get('ours'))[:12]}."
        checks.append(CheckOut(name="merge", status=status, message=message))
    else:
        checks.append(CheckOut(name="merge", status="pending", message="Not computed yet."))
    extraction = (row.checks or {}).get("extraction")
    if isinstance(extraction, dict):
        decisions = extraction.get("decisions") or {}
        pending = int(decisions.get("pending") or 0)
        checks.append(
            CheckOut(
                name="extraction",
                status="pending" if pending else "passed",
                message=f"{extraction.get('items', 0)} item(s): "
                f"{decisions.get('accepted', 0)} accepted, {pending} pending, "
                f"{decisions.get('rejected', 0)} rejected.",
            )
        )
    if approval is not None and approval.protected:
        checks.append(
            CheckOut(
                name="approvals",
                status="passed" if approval.satisfied else "pending",
                message=f"{len(approval.approvers)} of {approval.required} approval(s)"
                + (
                    f"; changes requested by {', '.join(approval.changes_requested)}"
                    if approval.changes_requested
                    else ""
                ),
            )
        )
    return checks


def _proposal(
    row: ProposalRow, approval: service.Approval | None = None, *, counted: bool = True
) -> ProposalOut:
    return ProposalOut(
        id=row.id,
        title=row.title,
        description=row.description,
        source_ref=row.source_ref,
        target_ref=row.target_ref,
        origin=row.origin,
        status=row.status,
        author=row.author,
        created_at=row.created_at,
        updated_at=row.updated_at,
        merged_revision=None if row.merged_revision is None else hex(row.merged_revision),
        policy=dict((row.policies or {}).get(service.POLICY_KEY) or {}),
        checks=_checks(row, approval, counted=counted),
    )


def _review(row: ReviewRow) -> ReviewOut:
    return ReviewOut(
        id=row.id, reviewer=row.reviewer, verdict=row.verdict, comment=row.comment, at=row.at
    )


def _approval(approval: service.Approval) -> ApprovalOut:
    return ApprovalOut(
        protected=approval.protected,
        required=approval.required,
        approvals=len(approval.approvers),
        approvers=list(approval.approvers),
        changes_requested=list(approval.changes_requested),
        satisfied=approval.satisfied,
    )


def _resolution(row: ResolutionRow, unmatched: frozenset[str]) -> ResolutionOut:
    body = row.resolution
    return ResolutionOut(
        fingerprint=row.fingerprint,
        choice=str(body.get("choice")),
        value=body.get("value"),
        values=body.get("values"),
        resolver=row.resolved_by,
        at=row.at,
        matched=row.fingerprint not in unmatched,
    )


def _attempt_fields(
    attempt: MergeAttemptRow,
    counts: dict[tuple[str, bool], int],
    resolutions: list[ResolutionRow],
    *,
    recomputed: bool,
    stale: bool,
) -> dict[str, Any]:
    outcome = attempt.outcome or {}
    unmatched = [str(f) for f in outcome.get("unmatched_resolutions") or []]
    per_code: dict[str, dict[str, int]] = {}
    for (code, resolved), count in sorted(counts.items()):
        entry = per_code.setdefault(code, {"open": 0, "resolved": 0})
        entry["resolved" if resolved else "open"] += count
    return {
        "id": attempt.id,
        "proposal": attempt.proposal_id,
        "status": attempt.status.value,
        "result": attempt.result,
        "base": None if attempt.base is None else hex(attempt.base),
        "ours": hex(attempt.ours),
        "theirs": hex(attempt.theirs),
        "virtual_base": attempt.virtual_base,
        "created_at": attempt.created_at,
        "created_by": attempt.created_by,
        "revision": None if attempt.revision is None else hex(attempt.revision),
        "policy": attempt.policy or {},
        "open_conflicts": sum(c["open"] for c in per_code.values()),
        "resolved_conflicts": sum(c["resolved"] for c in per_code.values()),
        "counts": per_code,
        "recomputed": recomputed,
        "stale": stale,
        "resolutions": [_resolution(r, frozenset(unmatched)) for r in resolutions],
        "unmatched_resolutions": unmatched,
        "auto_resolutions": list(outcome.get("auto_resolutions") or []),
    }


def _attempt_summary(attempt: MergeAttemptRow) -> AttemptOut:
    """Attempt without per-code counts and resolutions (Proposal detail)."""
    outcome = attempt.outcome or {}
    fields = _attempt_fields(attempt, {}, [], recomputed=False, stale=False)
    fields["open_conflicts"] = int(outcome.get("open") or 0)
    fields["resolved_conflicts"] = int(outcome.get("resolved") or 0)
    return AttemptOut(**fields)


def _conflict(row: ConflictRow) -> ConflictOut:
    return ConflictOut(
        id=row.fingerprint,
        status="resolved" if row.resolved else "open",
        resolution=row.resolution,
        **row.payload,
    )


def _conflicts(view: service.AttemptView) -> ConflictsOut:
    fields = _attempt_fields(
        view.attempt,
        view.counts,
        view.resolutions,
        recomputed=view.recomputed,
        stale=view.stale,
    )
    return ConflictsOut(
        **fields,
        conflicts=[_conflict(row) for row in view.conflicts],
        next_cursor=None if view.next_after is None else encode_cursor({"after": view.next_after}),
    )


def _detail(view: service.ProposalView, scope: ConflictScope) -> ProposalDetailOut:
    """`view`, whose attempt `service.scoped` limited to `scope`."""
    base = _proposal(view.proposal, view.approval, counted=scope.everything)
    return ProposalDetailOut(
        **base.model_dump(),
        reviews=[_review(r) for r in view.reviews],
        approval=_approval(view.approval),
        attempt=None if view.attempt is None else _attempt_summary(view.attempt),
    )


def _codes(kind: str | None) -> list[str]:
    if not kind:
        return []
    codes = [code.strip() for code in kind.split(",") if code.strip()]
    unknown = [code for code in codes if code not in _CODES]
    if unknown:
        raise ProblemError(
            VALIDATION_FAILED,
            "One or more request members are invalid.",
            extensions={
                "errors": [
                    {
                        "pointer": "/query/kind",
                        "message": f"unknown Conflict code {unknown[0]!r}",
                        "code": "invalid",
                    }
                ]
            },
        )
    return codes


async def _check(
    request: Request,
    auth: AuthDep,
    principal: PrincipalDep,
    permission: Permission,
    domain_id: str,
    branch: str,
) -> None:
    await authorize(request, auth, principal, permission, domain_id, AccessContext(branch=branch))


async def _readable(
    store: StoreDep, guard: ReadGuard, domain_id: str, proposal_id: str
) -> ConflictScope:
    """What the caller may read of the Proposal (403 when nothing; unknown Proposals are
    403 for callers whose grants are narrowed)."""
    await guard.precheck()
    try:
        proposal = await service.target_of(store, domain_id, proposal_id)
    except ProblemError as error:
        raise guard.unknown(error, wide=await guard.wide()) from None
    return await conflict_scope(guard, proposal)


def _store_problem(error: CommitError, retry_after_s: int) -> ProblemError:
    return commit_problem(error, max_issues=1, retry_after_s=retry_after_s)


# Routes -----------------------------------------------------------------------------------


@router.post(PROPOSALS, status_code=201, summary="Create a Proposal", responses=_ERRORS)
async def create_proposal(
    domain_id: str,
    body: ProposalCreate,
    request: Request,
    response: Response,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    idempotency_key: IdempotencyKeyDep,
    guard: DataRead,
) -> ProposalDetailOut:
    """Open a Proposal and compute its merge preview (Conflict count).

    The merge policy comes from the Domain's `merge_policy` setting. With an
    `Idempotency-Key`, a retried create returns the Proposal with status 200 and
    `Idempotent-Replayed: true`.
    """
    await _check(request, auth, principal, Permission.PROPOSAL_CREATE, domain_id, body.source_ref)
    await require_target_read(guard, body.target_ref)
    created = await service.create(
        store,
        domain_id,
        service.NewProposal(
            source_ref=body.source_ref,
            target_ref=body.target_ref,
            title=body.title,
            description=body.description,
        ),
        principal,
        keeper.proposals,
        idempotency_key=idempotency_key,
    )
    proposal = created.view.proposal
    annotate(
        request,
        action="proposal.create",
        target=proposal.id,
        source=proposal.source_ref,
        target_branch=proposal.target_ref,
        replayed=created.replayed,
    )
    if created.replayed:
        response.status_code = 200
        response.headers[REPLAYED_HEADER] = "true"
    response.headers["Location"] = (
        f"{keeper.api_prefix}/domains/{domain_id}/proposals/{proposal.id}"
    )
    scope = await visible_scope(guard, proposal)
    return _detail(await service.scoped(store, domain_id, created.view, scope), scope)


@router.get(PROPOSALS, summary="List Proposals", responses=_ERRORS)
async def list_proposals(
    domain_id: str,
    store: StoreDep,
    params: PageParamsDep,
    guard: DataRead,
    status: ProposalStatus | None = None,
    origin: ProposalOrigin | None = None,
    source: Annotated[str | None, Query(description="Source branch.")] = None,
    target: Annotated[str | None, Query(description="Target branch.")] = None,
    author: str | None = None,
) -> Page[ProposalOut]:
    """Proposals the caller may read, newest first."""
    await guard.precheck()
    after = None
    if params.position is not None:
        try:
            after = (
                datetime.fromisoformat(str(params.position["at"])),
                str(params.position["id"]),
            )
        except (KeyError, ValueError):
            raise ProblemError(VALIDATION_FAILED, "The cursor is not a Proposal cursor.") from None
    rows = await service.page(
        store,
        domain_id,
        ProposalFilter(
            status=status, origin=origin, source_ref=source, target_ref=target, author=author
        ),
        limit=params.limit + 1,
        after=after,
    )
    more = len(rows) > params.limit
    rows = rows[: params.limit]
    next_cursor = (
        encode_cursor({"at": rows[-1].created_at.isoformat(), "id": rows[-1].id})
        if more and rows
        else None
    )
    items = [
        _proposal(r, counted=(await visible_scope(guard, r)).everything)
        for r in await readable_proposals(guard, rows)
    ]
    return Page[ProposalOut](items=items, next_cursor=next_cursor)


@router.get(PROPOSAL, summary="Get a Proposal", responses=_ERRORS)
async def get_proposal(
    domain_id: str,
    proposal_id: str,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    guard: DataRead,
) -> ProposalDetailOut:
    """The Proposal with its reviews, approval state and latest merge attempt."""
    scope = await _readable(store, guard, domain_id, proposal_id)
    view = await service.get(store, domain_id, proposal_id, keeper.proposals)
    return _detail(await service.scoped(store, domain_id, view, scope), scope)


async def _author_or_merger(
    request: Request,
    auth: AuthDep,
    principal: PrincipalDep,
    store: StoreDep,
    domain_id: str,
    proposal_id: str,
) -> None:
    proposal = await service.target_of(store, domain_id, proposal_id)
    if principal.id != proposal.author:
        await _check(
            request, auth, principal, Permission.PROPOSAL_MERGE, domain_id, proposal.target_ref
        )


@router.patch(PROPOSAL, summary="Update a Proposal", responses=_ERRORS)
async def update_proposal(
    domain_id: str,
    proposal_id: str,
    body: ProposalUpdate,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    guard: DataRead,
    _: Annotated[Access, Depends(require(Permission.DATA_READ))],
) -> ProposalDetailOut:
    """Change the title or description of an open Proposal (its author or a maintainer)."""
    await _author_or_merger(request, auth, principal, store, domain_id, proposal_id)
    view = await service.update(
        store,
        domain_id,
        proposal_id,
        keeper.proposals,
        title=body.title,
        description=body.description,
    )
    annotate(
        request,
        action="proposal.update",
        target=proposal_id,
        fields=sorted(body.model_dump(exclude_unset=True)),
    )
    scope = await visible_scope(guard, view.proposal)
    return _detail(await service.scoped(store, domain_id, view, scope), scope)


@router.post(PROPOSAL + "/close", summary="Close a Proposal", responses=_ERRORS)
async def close_proposal(
    domain_id: str,
    proposal_id: str,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    guard: DataRead,
    _: Annotated[Access, Depends(require(Permission.DATA_READ))],
) -> ProposalDetailOut:
    """Close without merging; the merge attempt is discarded and the target is unchanged."""
    await _author_or_merger(request, auth, principal, store, domain_id, proposal_id)
    view = await service.close(store, domain_id, proposal_id, keeper.proposals)
    annotate(request, action="proposal.close", target=proposal_id)
    scope = await visible_scope(guard, view.proposal)
    return _detail(await service.scoped(store, domain_id, view, scope), scope)


@router.post(PROPOSAL + "/reviews", status_code=201, summary="Review a Proposal", responses=_ERRORS)
async def review_proposal(
    domain_id: str,
    proposal_id: str,
    body: ReviewIn,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
) -> ReviewResult:
    """Approve, request changes or comment."""
    proposal = await service.target_of(store, domain_id, proposal_id)
    await _check(
        request, auth, principal, Permission.PROPOSAL_REVIEW, domain_id, proposal.target_ref
    )
    row, view = await service.review(
        store,
        domain_id,
        proposal_id,
        principal,
        keeper.proposals,
        verdict=body.verdict,
        comment=body.comment,
    )
    annotate(request, action="proposal.review", target=proposal_id, verdict=body.verdict.value)
    return ReviewResult(review=_review(row), approval=_approval(view.approval))


class MergeIn(BaseModel):
    message: str | None = Field(default=None, max_length=2000)


@router.post(PROPOSAL + "/merge", summary="Merge a Proposal", responses=_ERRORS)
async def merge_proposal(
    domain_id: str,
    proposal_id: str,
    request: Request,
    response: Response,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    idempotency_key: IdempotencyKeyDep,
    guard: DataRead,
    body: MergeIn | None = None,
) -> MergeOut:
    """Run or re-run the merge with the stored resolutions.

    200 with the new target head when the merge committed (merge Revision with parents
    `[target head, source head]`, fast-forward, or nothing to do); 409 `merge-conflicts`
    with the attempt when Conflicts remain. With an `Idempotency-Key`, retrying a merge that
    committed returns it again (`Idempotent-Replayed: true`).
    """
    proposal = await service.target_of(store, domain_id, proposal_id)
    await _check(
        request, auth, principal, Permission.PROPOSAL_MERGE, domain_id, proposal.target_ref
    )
    scope = await conflict_scope(guard, proposal)
    try:
        outcome = await service.merge(
            store,
            domain_id,
            proposal_id,
            principal,
            keeper.proposals,
            idempotency_key=idempotency_key,
            message=None if body is None else body.message,
            page_limit=keeper.pagination.default_limit,
            scope=scope,
        )
    except CommitError as error:
        raise _store_problem(error, keeper.branches.lock_retry_after_s) from None
    attempt = _conflicts(outcome.view)
    annotate(
        request,
        action="proposal.merge",
        target=proposal_id,
        attempt=outcome.view.attempt.id,
        result=outcome.view.attempt.result,
        revision=None if outcome.head is None else hex(outcome.head),
        open_conflicts=attempt.open_conflicts,
        replayed=outcome.replayed,
    )
    if outcome.head is None:
        raise ProblemError(
            service.MERGE_CONFLICTS,
            f"{attempt.open_conflicts} Conflict(s) remain; resolve them and merge again.",
            extensions={"attempt": attempt.model_dump(mode="json")},
        )
    if outcome.replayed:
        response.headers[REPLAYED_HEADER] = "true"
    return MergeOut(
        result=outcome.view.attempt.result or "merged",
        revision=hex(outcome.head),
        proposal=_proposal(outcome.proposal, counted=scope.everything),
        attempt=attempt,
        source_deleted=outcome.source_deleted,
        replayed=outcome.replayed,
    )


@router.get(PROPOSAL + "/conflicts", summary="List the Conflicts of a Proposal", responses=_ERRORS)
async def list_conflicts(
    domain_id: str,
    proposal_id: str,
    principal: PrincipalDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    params: PageParamsDep,
    guard: DataRead,
    kind: Annotated[
        str | None, Query(description="Conflict codes, comma-separated (`DV1,XS8`).")
    ] = None,
    status: Annotated[Literal["open", "resolved"] | None, Query()] = None,
) -> ConflictsOut:
    """The current merge attempt with one page of its Conflicts, ordered by id."""
    scope = await _readable(store, guard, domain_id, proposal_id)
    after = None if params.position is None else str(params.position.get("after", "")) or None
    try:
        view = await service.conflicts(
            store,
            domain_id,
            proposal_id,
            principal,
            keeper.proposals,
            service.ConflictQuery(
                codes=_codes(kind),
                resolved=None if status is None else status == "resolved",
                limit=params.limit,
                after=after,
                scope=scope,
            ),
        )
    except CommitError as error:
        raise _store_problem(error, keeper.branches.lock_retry_after_s) from None
    return _conflicts(view)


@router.post(
    PROPOSAL + "/conflicts/{conflict_id}/resolution",
    summary="Resolve one Conflict",
    responses=_ERRORS,
)
async def resolve_conflict(
    domain_id: str,
    proposal_id: str,
    conflict_id: str,
    body: ResolutionIn,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    guard: DataRead,
) -> ResolutionOut:
    """Store the resolution of one Conflict of the current attempt (no merge run)."""
    proposal = await service.target_of(store, domain_id, proposal_id)
    await _check(
        request, auth, principal, Permission.CONFLICT_RESOLVE, domain_id, proposal.target_ref
    )
    scope = await conflict_scope(guard, proposal)
    try:
        rows, _view = await service.resolve(
            store,
            domain_id,
            proposal_id,
            principal,
            keeper.proposals,
            [
                service.ResolutionInput(
                    fingerprint=conflict_id,
                    choice=body.choice,
                    value=body.value,
                    values=body.values,
                )
            ],
            rerun=False,
            page_limit=keeper.pagination.default_limit,
            scope=scope,
        )
    except CommitError as error:
        raise _store_problem(error, keeper.branches.lock_retry_after_s) from None
    annotate(request, action="proposal.resolve", target=proposal_id, conflicts=[conflict_id])
    return _resolution(rows[0], frozenset())


@router.post(
    PROPOSAL + "/conflicts:resolve", summary="Resolve Conflicts in bulk", responses=_ERRORS
)
async def resolve_conflicts(
    domain_id: str,
    proposal_id: str,
    body: ResolveManyIn,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    guard: DataRead,
) -> ConflictsOut:
    """Store several resolutions, re-run the merge and return the new attempt."""
    if len(body.resolutions) > keeper.proposals.max_resolutions:
        raise ProblemError(
            VALIDATION_FAILED,
            f"At most {keeper.proposals.max_resolutions} resolutions per request.",
        )
    proposal = await service.target_of(store, domain_id, proposal_id)
    await _check(
        request, auth, principal, Permission.CONFLICT_RESOLVE, domain_id, proposal.target_ref
    )
    scope = await conflict_scope(guard, proposal)
    try:
        _rows, view = await service.resolve(
            store,
            domain_id,
            proposal_id,
            principal,
            keeper.proposals,
            [
                service.ResolutionInput(
                    fingerprint=item.fingerprint,
                    choice=item.choice,
                    value=item.value,
                    values=item.values,
                    pointer=f"/body/resolutions/{index}",
                )
                for index, item in enumerate(body.resolutions)
            ],
            rerun=True,
            page_limit=keeper.pagination.default_limit,
            scope=scope,
        )
    except CommitError as error:
        raise _store_problem(error, keeper.branches.lock_retry_after_s) from None
    assert view is not None
    annotate(
        request,
        action="proposal.resolve",
        target=proposal_id,
        conflicts=[item.fingerprint for item in body.resolutions],
        attempt=view.attempt.id,
    )
    return _conflicts(view)
