"""Proposals, reviews, merges and Conflict resolution (design 6.8 to 6.10, 10.3, 11.1).

| Method and path | Permission | Notes |
|-----------------|------------|-------|
| `POST .../proposals` | `proposal.create` (source), `data.read` (target) | 201; key replays 200 |
| `GET .../proposals` | `data.read` | filters status, origin, source, target, author, assigned |
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

Every Proposal carries its steward roles and, for callers with `principal.read`, its
assignees (`services.stewards`, design 10.7); `assigned=me|any|none` keeps the Proposals the
caller is an assignee of, those with steward roles, or those without.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field, JsonValue

from lorebase_core.canonical import hex
from lorebase_core.merge import ConflictKind
from lorebase_core.model import Schema, Sensitivity
from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import Access, AuthDep, PrincipalDep, authorize, require
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.rbac import AccessContext
from lorebase_keeper.context import StoreDep
from lorebase_keeper.deps import IdempotencyKeyDep, KeeperSettingsDep
from lorebase_keeper.errors import VALIDATION_FAILED, Problem, ProblemError, ValidationProblem
from lorebase_keeper.pagination import Page, PageParamsDep, encode_cursor
from lorebase_keeper.reading.points import read_guard
from lorebase_keeper.reading.sensitivity import (
    Disclosure,
    Masking,
    collapse_issues,
    conflict_hidden,
    default_schema,
    mask_conflict,
    masking_for,
)
from lorebase_keeper.reading.visibility import projection
from lorebase_keeper.routes.domains import REPLAYED_HEADER
from lorebase_keeper.services import agent_writes
from lorebase_keeper.services import proposals as service
from lorebase_keeper.services.errors import commit_problem, findings_of
from lorebase_keeper.services.pii_import import require_schema_change
from lorebase_keeper.services.proposal_reads import (
    ConflictScope,
    conflict_scope,
    readable_proposals,
    require_target_read,
    visible_scope,
)
from lorebase_keeper.services.reads import ReadGuard
from lorebase_keeper.services.stewards import (
    Assigned,
    AssigneeList,
    Stewards,
    proposal_subject,
)
from lorebase_keeper.settings import KeeperSettings
from lorebase_store.commit import CommitError, ValidationFailedError
from lorebase_store.commit.schema import load_compiled
from lorebase_store.merging import proposal_kinds
from lorebase_store.models.enums import ProposalOrigin, ProposalStatus, ReviewVerdict
from lorebase_store.reads import head_schema
from lorebase_store.repositories import (
    ConflictRow,
    MaterializedRepository,
    MergeAttemptRow,
    ProposalFilter,
    ProposalRow,
    ResolutionRow,
    ReviewRow,
    scope_to_domain,
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
        "(`approvals-required`), Conflicts remain (`merge-conflicts`, with `attempt`), open "
        "contradictions block it (`reconciliation-open`, with `candidates`), too few stewards "
        "can approve it (`stewards-unavailable`), a branch is gone "
        "(`proposal-branch-missing`), duplicate or idempotency conflict.",
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
    candidates: list[str] | None = Field(
        default=None,
        description="`reconciliation`: ids of the first open candidates of the Proposal.",
    )


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
    steward_roles: list[str] = Field(
        default_factory=list,
        description="Roles that look after the Kinds the changes touch (design 10.7).",
    )
    assignees: list[str] | None = Field(
        default=None,
        description="Principals who can review it as stewards (callers with principal.read "
        "only; at most keeper.stewards.max_assignees_listed).",
    )
    assignees_truncated: bool = False


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
    required_from_stewards: int | None = Field(
        default=None,
        description="Approvals that must come from the Proposal's assignees (protection rule "
        "required_approvals_from_stewards); null when the rule does not apply.",
    )
    steward_approvals: int | None = Field(
        default=None, description="Approvals given by assignees, when the rule applies."
    )
    stewards_unavailable: bool = Field(
        default=False,
        description="Steward roles are declared but fewer principals can approve than the "
        "rule requires (merge refused with stewards-unavailable).",
    )
    required_roles: list[str] | None = Field(
        default=None,
        description="Roles that must each have approved (protection rule required_roles); "
        "null when the rule does not apply.",
    )
    missing_roles: list[str] | None = Field(
        default=None,
        description="Required roles without a counted approval yet, when the rule applies.",
    )


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
    """The checks of `row`; `counted` false leaves the number of open Conflicts and of
    extraction items out (callers who do not see every Conflict: items may be about Kinds
    they may not read)."""
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
                message=(
                    f"{extraction.get('items', 0)} item(s): "
                    f"{decisions.get('accepted', 0)} accepted, {pending} pending, "
                    f"{decisions.get('rejected', 0)} rejected."
                    if counted
                    else "Items pending review."
                    if pending
                    else "Items reviewed."
                ),
            )
        )
    reconciliation = (row.checks or {}).get("reconciliation")
    if isinstance(reconciliation, dict):
        opened = int(reconciliation.get("open") or 0)
        blocking = int(reconciliation.get("blocking") or 0)
        rstatus: Literal["passed", "failed", "pending"] = (
            "failed" if blocking else "pending" if opened else "passed"
        )
        if not opened:
            text = "No open reconciliation candidates."
        elif counted:
            text = f"{opened} open reconciliation candidate(s)" + (
                f", {blocking} contradiction(s) blocking the merge." if blocking else "."
            )
        else:
            text = "Open reconciliation candidates."
        checks.append(
            CheckOut(
                name="reconciliation",
                status=rstatus,
                message=text,
                candidates=[str(i) for i in reconciliation.get("candidates") or []]
                if counted
                else None,
            )
        )
    if approval is not None and approval.protected:
        rule = approval.stewards
        steward_text = ""
        if rule is not None and rule.roles:
            steward_text = (
                f"; stewards unavailable: {rule.assignees} of {rule.required} needed"
                if rule.unavailable
                else f"; {len(rule.approvers)} of {rule.required} from stewards"
            )
        checks.append(
            CheckOut(
                name="approvals",
                status="passed"
                if approval.satisfied
                else "failed"
                if rule is not None and rule.unavailable
                else "pending",
                message=f"{len(approval.approvers)} of {approval.required} approval(s)"
                + steward_text
                + (
                    f"; missing roles: {', '.join(approval.roles.missing)}"
                    if approval.roles is not None and approval.roles.missing
                    else ""
                )
                + (
                    f"; changes requested by {', '.join(approval.changes_requested)}"
                    if approval.changes_requested
                    else ""
                ),
            )
        )
    return checks


@dataclass(frozen=True)
class _Assignment:
    """Steward roles, shown assignees and whether the caller is an assignee."""

    roles: tuple[str, ...]
    assignees: AssigneeList
    mine: bool


async def _assignments(
    store: StoreDep,
    stewards: Stewards,
    principal: Any,
    domain_id: str,
    rows: list[ProposalRow],
) -> dict[str, _Assignment]:
    """The steward assignment of each Proposal, by the schema of its target's head."""
    schemas: dict[str, Schema | None] = {}
    kinds: dict[str, tuple[str, ...] | None] = {}
    async with store.sessions() as session, session.begin():
        await scope_to_domain(session, domain_id)
        for target in sorted({row.target_ref for row in rows}):
            head = await head_schema(session, domain_id, target, runtime=store.runtime)
            schemas[target] = None if head is None else head.schema
        for row in rows:
            kinds[row.id] = await proposal_kinds(session, domain_id, row)
    found: dict[str, _Assignment] = {}
    for row in rows:
        subject = proposal_subject(schemas[row.target_ref], kinds[row.id], row.target_ref)
        found[row.id] = _Assignment(
            roles=subject.roles,
            assignees=await stewards.listing(principal, domain_id, subject),
            mine=await stewards.is_assignee(principal, domain_id, subject),
        )
    return found


def _stewards(auth: Any, keeper: KeeperSettings) -> Stewards:
    return Stewards(auth, keeper.stewards)


def _proposal(
    row: ProposalRow,
    approval: service.Approval | None = None,
    *,
    counted: bool = True,
    assignment: _Assignment | None = None,
) -> ProposalOut:
    shown = (
        {}
        if assignment is None
        else {
            "steward_roles": list(assignment.roles),
            "assignees": None
            if assignment.assignees.ids is None
            else list(assignment.assignees.ids),
            "assignees_truncated": assignment.assignees.truncated,
        }
    )
    return ProposalOut(
        **shown,
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
    rule = approval.stewards
    return ApprovalOut(
        protected=approval.protected,
        required=approval.required,
        approvals=len(approval.approvers),
        approvers=list(approval.approvers),
        changes_requested=list(approval.changes_requested),
        satisfied=approval.satisfied,
        required_from_stewards=None if rule is None else rule.required,
        steward_approvals=None if rule is None else len(rule.approvers),
        stewards_unavailable=rule is not None and rule.unavailable,
        required_roles=None if approval.roles is None else list(approval.roles.roles),
        missing_roles=None if approval.roles is None else list(approval.roles.missing),
    )


def _resolution(
    row: ResolutionRow, unmatched: frozenset[str], masked: str | None = None
) -> ResolutionOut:
    """A stored resolution; with `masked`, its custom values are replaced by it."""
    body = row.resolution
    value, values = body.get("value"), body.get("values")
    if masked is not None:
        value = None if value is None else masked
        values = None if values is None else dict.fromkeys(values, masked)
    return ResolutionOut(
        fingerprint=row.fingerprint,
        choice=str(body.get("choice")),
        value=value,
        values=values,
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
    masked: Callable[[str], str | None] = lambda _fingerprint: None,
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
        "resolutions": [
            _resolution(r, frozenset(unmatched), masked(r.fingerprint)) for r in resolutions
        ],
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


@dataclass(frozen=True)
class _Visibility:
    """What the caller may see of Conflict values: the target branch's schema (None when it
    is not materialized) and the caller's masking."""

    schema: Schema | None
    masking: Masking
    left_out: frozenset[str] = frozenset()
    """Ids the caller's projection of the schema leaves out (Kinds invisible to it, design
    13.3): Conflicts and automatic resolutions never show them."""

    def hidden(self, payload: Mapping[str, Any]) -> bool:
        return conflict_hidden(payload, self.schema, self.masking)

    def names_left_out(self, value: Any) -> bool:
        if not self.left_out:
            return False
        text = json.dumps(value, default=str)
        return any(i in text for i in self.left_out)

    def scrub(self, payload: dict[str, Any]) -> dict[str, Any]:
        """`payload` without the values of Attributes left out, and without a message or
        details naming something left out."""
        if not self.left_out:
            return payload
        out = dict(payload)
        for name in (*_CONFLICT_SIDES, "values"):
            out[name] = _without(out.get(name), self.left_out)
        if self.names_left_out([out.get("message"), out.get("details")]):
            out["message"] = "The versions differ."
            out["details"] = {}
        return out


_CONFLICT_SIDES = ("base", "ours", "theirs")


def _without(value: Any, ids: frozenset[str]) -> Any:
    """A value map (or an element body holding `values`) without the entries of `ids`."""
    if not isinstance(value, dict):
        return value
    if isinstance(value.get("values"), dict):
        return {**value, "values": _without(value["values"], ids)}
    return {k: v for k, v in value.items() if k not in ids}


async def _visibility(
    store: StoreDep,
    guard: ReadGuard,
    domain_id: str,
    target: str,
    keeper: KeeperSettings,
    scope: ConflictScope | None = None,
) -> _Visibility:
    writes = store.runtime.settings.writes
    async with store.sessions() as session, session.begin():
        await scope_to_domain(session, domain_id)
        compiled = await MaterializedRepository(session, domain_id, target, writes=writes).schema()
        reference = await default_schema(session, domain_id, store.runtime)
    schema = None if compiled is None else load_compiled(compiled.compiled)
    masking = await masking_for(
        guard.authorizer, guard.principal, domain_id, reference, keeper.reads.sensitivity
    )
    left_out: frozenset[str] = frozenset()
    if schema is not None and scope is not None and scope.invisible and scope.kinds is not None:
        left_out = projection(schema, scope.kinds).hidden
    return _Visibility(schema, masking if schema is None else masking.over(schema), left_out)


def _conflict(row: ConflictRow, seen: _Visibility) -> ConflictOut:
    payload = seen.scrub(mask_conflict(row.payload, seen.schema, seen.masking))
    resolution = None if row.resolution is None else seen.scrub(row.resolution)
    if resolution is not None and seen.hidden(row.payload):
        resolution = {
            **resolution,
            **{k: seen.masking.masked for k in ("value", "values") if resolution.get(k)},
        }
    return ConflictOut(
        id=row.fingerprint,
        status="resolved" if row.resolved else "open",
        resolution=resolution,
        **payload,
    )


async def _attempt(
    view: service.AttemptView,
    store: StoreDep,
    guard: ReadGuard,
    domain_id: str,
    target: str,
    keeper: KeeperSettings,
    scope: ConflictScope | None = None,
) -> ConflictsOut:
    """The attempt as the caller may see it: values masked by schema (`_conflicts`), then
    the vault references left in it resolved as on every read (`Disclosure`)."""
    seen = await _visibility(store, guard, domain_id, target, keeper, scope)
    attempt = _conflicts(view, seen)
    schemas = () if seen.schema is None else (seen.schema,)
    async with store.sessions() as session, session.begin():
        await scope_to_domain(session, domain_id)
        disclosure = Disclosure(session, domain_id, schemas, seen.masking, store.runtime.vault)
        body = await disclosure.model(attempt)
    return ConflictsOut.model_validate_json(body)


def _conflicts(view: service.AttemptView, seen: _Visibility) -> ConflictsOut:
    hidden = {row.fingerprint for row in view.conflicts if seen.hidden(row.payload)}
    shown = {row.fingerprint for row in view.conflicts} - hidden
    full = seen.masking.clearance.covers(Sensitivity, ())

    def masked(fingerprint: str) -> str | None:
        # Resolutions of Conflicts outside this page are masked unless the caller is cleared
        # for everything: their Attribute is not known here.
        if fingerprint in shown or (fingerprint not in hidden and full):
            return None
        return seen.masking.masked

    fields = _attempt_fields(
        view.attempt,
        view.counts,
        view.resolutions,
        recomputed=view.recomputed,
        stale=view.stale,
        masked=masked,
    )
    fields["auto_resolutions"] = [
        a for a in fields["auto_resolutions"] if not seen.names_left_out(a)
    ]
    return ConflictsOut(
        **fields,
        conflicts=[_conflict(row, seen) for row in view.conflicts],
        next_cursor=None if view.next_after is None else encode_cursor({"after": view.next_after}),
    )


def _detail(
    view: service.ProposalView, scope: ConflictScope, assignment: _Assignment | None = None
) -> ProposalDetailOut:
    """`view`, whose attempt `service.scoped` limited to `scope`."""
    base = _proposal(view.proposal, view.approval, counted=scope.everything, assignment=assignment)
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


async def _store_problem(
    error: CommitError, retry_after_s: int, seen: Callable[[], Awaitable[_Visibility]]
) -> ProblemError:
    """The problem answering a store failure; validation issues about values the caller may
    not read (`seen`) have a generic code."""
    if not isinstance(error, ValidationFailedError):
        return commit_problem(error, max_issues=1, retry_after_s=retry_after_s)
    visible = await seen()
    schemas = () if visible.schema is None else (visible.schema,)
    return commit_problem(
        error,
        max_issues=1,
        retry_after_s=retry_after_s,
        locate=lambda issues: findings_of(collapse_issues(issues, schemas, visible.masking)),
    )


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
        reconciliation_check=keeper.reconciliation.merge_check,
        stewards=_stewards(auth, keeper),
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
    stewards = _stewards(auth, keeper)
    assigned = await _assignments(store, stewards, principal, domain_id, [proposal])
    return _detail(
        await service.scoped(store, domain_id, created.view, scope), scope, assigned[proposal.id]
    )


@router.get(PROPOSALS, summary="List Proposals", responses=_ERRORS)
async def list_proposals(
    domain_id: str,
    store: StoreDep,
    params: PageParamsDep,
    guard: DataRead,
    auth: AuthDep,
    keeper: KeeperSettingsDep,
    status: ProposalStatus | None = None,
    origin: ProposalOrigin | None = None,
    source: Annotated[str | None, Query(description="Source branch.")] = None,
    target: Annotated[str | None, Query(description="Target branch.")] = None,
    author: str | None = None,
    assigned: Annotated[
        Assigned | None,
        Query(
            description="`me`: Proposals the caller is an assignee of; `any`: with steward "
            "roles; `none`: without (design 10.7)."
        ),
    ] = None,
) -> Page[ProposalOut]:
    """Proposals the caller may read, newest first. With `assigned`, a page may hold fewer
    items than asked while `next_cursor` is set."""
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
    readable = await readable_proposals(guard, rows)
    stewards = _stewards(auth, keeper)
    assignments = await _assignments(store, stewards, guard.principal, domain_id, readable)
    items = [
        _proposal(
            r, counted=(await visible_scope(guard, r)).everything, assignment=assignments[r.id]
        )
        for r in readable
        if _passes(assignments[r.id], assigned)
    ]
    return Page[ProposalOut](items=items, next_cursor=next_cursor)


def _passes(assignment: _Assignment, assigned: Assigned | None) -> bool:
    if assigned is None:
        return True
    if assigned == "me":
        return assignment.mine
    return bool(assignment.roles) == (assigned == "any")


@router.get(PROPOSAL, summary="Get a Proposal", responses=_ERRORS)
async def get_proposal(
    domain_id: str,
    proposal_id: str,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    guard: DataRead,
    auth: AuthDep,
) -> ProposalDetailOut:
    """The Proposal with its reviews, approval state, latest merge attempt and stewards."""
    scope = await _readable(store, guard, domain_id, proposal_id)
    stewards = _stewards(auth, keeper)
    view = await service.get(store, domain_id, proposal_id, keeper.proposals, stewards=stewards)
    assigned = await _assignments(store, stewards, guard.principal, domain_id, [view.proposal])
    return _detail(
        await service.scoped(store, domain_id, view, scope), scope, assigned[view.proposal.id]
    )


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
        stewards=_stewards(auth, keeper),
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
    view = await service.close(
        store, domain_id, proposal_id, keeper.proposals, stewards=_stewards(auth, keeper)
    )
    released = await agent_writes.release_branch(store, domain_id, view.proposal, keeper)
    annotate(request, action="proposal.close", target=proposal_id, branch_deleted=released)
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
        stewards=_stewards(auth, keeper),
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
            schema_guard=lambda old, new: require_schema_change(
                auth.authorizer, principal, domain_id, old, new
            ),
            reconciliation_check=keeper.reconciliation.merge_check,
            stewards=_stewards(auth, keeper),
        )
    except CommitError as error:
        raise await _store_problem(
            error,
            keeper.branches.lock_retry_after_s,
            lambda: _visibility(store, guard, domain_id, proposal.target_ref, keeper, scope),
        ) from None
    attempt = await _attempt(
        outcome.view, store, guard, domain_id, proposal.target_ref, keeper, scope
    )
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
    source_deleted = outcome.source_deleted
    if not outcome.replayed and not source_deleted:
        source_deleted = await agent_writes.release_branch(
            store, domain_id, outcome.proposal, keeper
        )
        annotate(request, branch_deleted=source_deleted)
    if outcome.replayed:
        response.headers[REPLAYED_HEADER] = "true"
    return MergeOut(
        result=outcome.view.attempt.result or "merged",
        revision=hex(outcome.head),
        proposal=_proposal(outcome.proposal, counted=scope.everything),
        attempt=attempt,
        source_deleted=source_deleted,
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
    proposal = await service.target_of(store, domain_id, proposal_id)
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
            reconciliation_check=keeper.reconciliation.merge_check,
        )
    except CommitError as error:
        raise await _store_problem(
            error,
            keeper.branches.lock_retry_after_s,
            lambda: _visibility(store, guard, domain_id, proposal.target_ref, keeper, scope),
        ) from None
    return await _attempt(view, store, guard, domain_id, proposal.target_ref, keeper, scope)


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
            reconciliation_check=keeper.reconciliation.merge_check,
        )
    except CommitError as error:
        raise await _store_problem(
            error,
            keeper.branches.lock_retry_after_s,
            lambda: _visibility(store, guard, domain_id, proposal.target_ref, keeper, scope),
        ) from None
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
            reconciliation_check=keeper.reconciliation.merge_check,
        )
    except CommitError as error:
        raise await _store_problem(
            error,
            keeper.branches.lock_retry_after_s,
            lambda: _visibility(store, guard, domain_id, proposal.target_ref, keeper, scope),
        ) from None
    assert view is not None
    annotate(
        request,
        action="proposal.resolve",
        target=proposal_id,
        conflicts=[item.fingerprint for item in body.resolutions],
        attempt=view.attempt.id,
    )
    return await _attempt(view, store, guard, domain_id, proposal.target_ref, keeper, scope)
