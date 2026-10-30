"""Proposal use cases (design 6.8 to 6.10, 10.3): create, read, review, merge, resolve, close.

Authorization happens in the routes; this module enforces the Proposal lifecycle and the
review policy:

- a Proposal is `open` until it is merged or closed; only open Proposals are reviewed,
  resolved and merged;
- merging into a protected branch needs `required_approvals` approvals (setting
  `keeper.proposals.required_approvals`, overridden by the Domain setting of the same name):
  the latest approve or request-changes verdict of each reviewer counts, the author's only
  when self-approval is allowed, and an outstanding request for changes blocks the merge;
- `required_approvals_from_stewards` (setting and Domain setting of that name, default 0) of
  those approvals must come from the Proposal's assignees (`services.stewards`, design 10.7):
  the Proposal then needs the larger of the two in all. A Proposal whose Kinds declare no
  stewards satisfies it; one with steward roles but fewer assignees than required cannot merge
  (409 `stewards-unavailable`);
- `required_roles` (setting and Domain setting of that name, default none) lists RBAC roles
  that must each have approved: at least one counted approval from a principal holding the
  role through a grant covering the Domain who may review the Proposal on the target (the
  assignee rule of `services.stewards`, for that role alone). It does not raise the approvals
  count; a missing role refuses the merge (409 `approvals-required`, naming the roles). Without
  a way to find those principals no role is satisfied;
- the merge policy of a Proposal is the Domain's `merge_policy` setting when it is created;
- the `reconciliation` check (`keeper.reconciliation.merge_check`, or the Domain setting
  `reconciliation_check` when that is stricter) lists the open reconciliation candidates of a
  Proposal and, in `block` mode, refuses its merge while contradictions are open (409
  `reconciliation-open`); a merge that commits resolves the candidates whose decision opened
  the Proposal (`reconciliation_settle`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from pydantic import JsonValue, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.merge import Conflict, MergePolicy, Resolution, ResolutionError
from lorebase_core.model import new_ulid
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.context import StoreAccess, require_branch, require_domain, require_revision
from lorebase_keeper.errors import NOT_FOUND, VALIDATION_FAILED, ProblemError, ProblemType
from lorebase_keeper.services.config import ProposalSettings
from lorebase_keeper.services.errors import ALREADY_EXISTS, IDEMPOTENCY_CONFLICT
from lorebase_keeper.services.proposal_reads import (
    FULL_SCOPE,
    ConflictScope,
    require_visible,
    unknown_conflict,
    visible_partitions,
)
from lorebase_keeper.services.reconciliation_settle import settle_merged
from lorebase_keeper.services.schema import SchemaGuard, schema_at
from lorebase_keeper.services.stewards import Stewards, proposal_subject
from lorebase_store.approval_rules import effective_rules
from lorebase_store.merging import (
    AttemptResult,
    MergeRequest,
    MergeService,
    ProposalBranchMissingError,
    check_resolution,
    proposal_kinds,
)
from lorebase_store.models.enums import (
    MergeAttemptStatus,
    ProposalOrigin,
    ProposalStatus,
    ReviewVerdict,
)
from lorebase_store.reads import head_schema
from lorebase_store.repositories import (
    EVERY_PARTITION,
    ConflictRepository,
    ConflictRow,
    DomainRow,
    MergeAttemptRepository,
    MergeAttemptRow,
    PartitionFilter,
    ProposalFilter,
    ProposalRepository,
    ProposalRow,
    RefRepository,
    ResolutionRepository,
    ResolutionRow,
    ReviewRepository,
    ReviewRow,
)
from lorebase_store.runtime import StoreRuntime

PROPOSAL_PREFIX = "prp"
REVIEW_PREFIX = "prr"
POLICY_KEY = "merge_policy"
"""Key of the merge policy in a Domain's settings and in a Proposal's `policies`."""

PROPOSAL_NOT_OPEN = ProblemType("proposal-not-open", 409, "Proposal is not open")
APPROVALS_REQUIRED = ProblemType("approvals-required", 409, "Approvals required")
MERGE_CONFLICTS = ProblemType("merge-conflicts", 409, "Merge has Conflicts")
SELF_APPROVAL = ProblemType("self-approval", 403, "Self-approval is not allowed")
BRANCH_MISSING = ProblemType("proposal-branch-missing", 409, "Proposal branch missing")
RECONCILIATION_OPEN = ProblemType(
    "reconciliation-open", 409, "Open contradictions between Sources block the merge"
)
STEWARDS_UNAVAILABLE = ProblemType(
    "stewards-unavailable", 409, "Fewer stewards can approve than the branch requires"
)


@dataclass(frozen=True)
class NewProposal:
    source_ref: str
    target_ref: str
    title: str
    description: str | None = None
    origin: ProposalOrigin = ProposalOrigin.MANUAL


@dataclass(frozen=True)
class StewardApproval:
    """The steward rule of a protected target (design 10.7): `required` approvals from the
    `assignees` of the Proposal's steward `roles`; `approvers` are the assignees who
    approved."""

    required: int
    roles: tuple[str, ...]
    assignees: int
    approvers: tuple[str, ...]

    @property
    def unavailable(self) -> bool:
        """Steward roles are declared but too few principals can approve."""
        return bool(self.roles) and self.assignees < self.required

    @property
    def satisfied(self) -> bool:
        return not self.roles or len(self.approvers) >= self.required


@dataclass(frozen=True)
class RoleApproval:
    """The role rule of a protected target: each of `roles` needs an approval; `approvers`
    maps each role to the approvers holding it."""

    roles: tuple[str, ...]
    approvers: dict[str, tuple[str, ...]]

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(role for role in self.roles if not self.approvers.get(role))

    @property
    def satisfied(self) -> bool:
        return not self.missing


@dataclass(frozen=True)
class Approval:
    """Review state of a Proposal against its target's protection."""

    protected: bool
    required: int
    approvers: tuple[str, ...]
    changes_requested: tuple[str, ...]
    blocking: bool
    stewards: StewardApproval | None = None
    roles: RoleApproval | None = None

    @property
    def satisfied(self) -> bool:
        if not self.protected:
            return True
        blocked = self.blocking and bool(self.changes_requested)
        stewards = self.stewards is None or self.stewards.satisfied
        roles = self.roles is None or self.roles.satisfied
        return len(self.approvers) >= self.required and not blocked and stewards and roles


@dataclass(frozen=True)
class ProposalView:
    proposal: ProposalRow
    reviews: list[ReviewRow]
    approval: Approval
    attempt: MergeAttemptRow | None


@dataclass(frozen=True)
class CreatedProposal:
    view: ProposalView
    replayed: bool = False


@dataclass(frozen=True)
class AttemptView:
    """An attempt with one page of its Conflicts and the Proposal's resolutions."""

    proposal: ProposalRow
    attempt: MergeAttemptRow
    conflicts: list[ConflictRow]
    next_after: str | None
    counts: dict[tuple[str, bool], int]
    resolutions: list[ResolutionRow]
    recomputed: bool = False
    stale: bool = False


@dataclass(frozen=True)
class MergeOutcome:
    """Result of a merge request: merged (`head` set) or the attempt with open Conflicts."""

    proposal: ProposalRow
    result: AttemptResult | None
    view: AttemptView
    head: bytes | None
    replayed: bool = False
    source_deleted: bool = False


@dataclass(frozen=True)
class ConflictQuery:
    codes: Sequence[str] = ()
    resolved: bool | None = None
    limit: int = 100
    after: str | None = None
    scope: ConflictScope = field(default=FULL_SCOPE)


@dataclass(frozen=True)
class ResolutionInput:
    fingerprint: str
    choice: str
    value: JsonValue = None
    values: dict[str, JsonValue] | None = None
    pointer: str = "/body"


# Helpers ----------------------------------------------------------------------------------


def domain_policy(domain: DomainRow) -> MergePolicy:
    """The Domain's merge policy setting; 422 when it is not a valid policy."""
    raw = (domain.settings or {}).get(POLICY_KEY) or {}
    try:
        return MergePolicy.model_validate(raw)
    except ValidationError as error:
        raise ProblemError(
            VALIDATION_FAILED,
            f"The Domain setting {POLICY_KEY} is not a valid merge policy: "
            f"{error.errors()[0]['msg']}",
        ) from None


def proposal_policy(proposal: ProposalRow) -> MergePolicy:
    return MergePolicy.model_validate((proposal.policies or {}).get(POLICY_KEY) or {})


def required_approvals(domain: DomainRow, settings: ProposalSettings) -> int:
    return effective_rules(domain.settings, settings).approvals


def required_from_stewards(domain: DomainRow, settings: ProposalSettings) -> int:
    return effective_rules(domain.settings, settings).from_stewards


def required_role_names(domain: DomainRow, settings: ProposalSettings) -> tuple[str, ...]:
    return effective_rules(domain.settings, settings).roles


async def role_approval(
    session: AsyncSession,
    domain: DomainRow,
    proposal: ProposalRow,
    approval: Approval,
    settings: ProposalSettings,
    runtime: StoreRuntime | None,
    stewards: Stewards | None,
) -> RoleApproval | None:
    """The role rule of the Proposal (module doc); `None` when it does not apply. Without
    `stewards` (no way to find role holders) it fails closed: no approver is counted."""
    roles = required_role_names(domain, settings)
    if not roles or not approval.protected:
        return None
    if stewards is None:
        return RoleApproval(roles, {})
    head = await head_schema(session, domain.id, proposal.target_ref, runtime=runtime)
    kinds = await proposal_kinds(session, domain.id, proposal)
    subject = proposal_subject(None if head is None else head.schema, kinds, proposal.target_ref)
    approvers: dict[str, tuple[str, ...]] = {}
    for role in roles:
        holders = await stewards.assignees(domain.id, replace(subject, roles=(role,)))
        approvers[role] = tuple(a for a in approval.approvers if a in holders)
    return RoleApproval(roles, approvers)


async def steward_approval(
    session: AsyncSession,
    domain: DomainRow,
    proposal: ProposalRow,
    approval: Approval,
    settings: ProposalSettings,
    runtime: StoreRuntime | None,
    stewards: Stewards | None,
) -> StewardApproval | None:
    """The steward rule of the Proposal (module doc); `None` when it does not apply. Without
    `stewards` (no way to find assignees) it fails closed: none are counted."""
    required = required_from_stewards(domain, settings)
    if required == 0 or not approval.protected:
        return None
    head = await head_schema(session, domain.id, proposal.target_ref, runtime=runtime)
    # Computed when no attempt stored them yet (a Proposal opened before they were stored).
    kinds = await proposal_kinds(session, domain.id, proposal)
    subject = proposal_subject(None if head is None else head.schema, kinds, proposal.target_ref)
    if not subject.roles:
        return StewardApproval(required, (), 0, ())
    if stewards is None:
        return StewardApproval(required, subject.roles, 0, ())
    assignees = await stewards.assignees(domain.id, subject)
    return StewardApproval(
        required,
        subject.roles,
        len(assignees),
        tuple(a for a in approval.approvers if a in assignees),
    )


def approval_of(
    proposal: ProposalRow,
    reviews: Sequence[ReviewRow],
    *,
    protected: bool,
    required: int,
    settings: ProposalSettings,
) -> Approval:
    """Latest decisive verdict per reviewer (comments do not change it)."""
    latest: dict[str, ReviewVerdict] = {}
    for review in reviews:
        if review.verdict is ReviewVerdict.COMMENT:
            continue
        if review.reviewer == proposal.author and not settings.allow_self_approval:
            continue
        latest[review.reviewer] = review.verdict
    return Approval(
        protected=protected,
        required=required,
        approvers=tuple(sorted(r for r, v in latest.items() if v is ReviewVerdict.APPROVE)),
        changes_requested=tuple(
            sorted(r for r, v in latest.items() if v is ReviewVerdict.REQUEST_CHANGES)
        ),
        blocking=settings.changes_requested_blocks,
    )


def _not_found(proposal_id: str) -> ProblemError:
    return ProblemError(NOT_FOUND, f"Proposal {proposal_id!r} not found.")


def _not_open(proposal: ProposalRow) -> ProblemError:
    return ProblemError(
        PROPOSAL_NOT_OPEN,
        f"Proposal {proposal.id!r} is {proposal.status.value}.",
        extensions={"proposal": proposal.id, "status": proposal.status.value},
    )


def _branch_missing(error: ProposalBranchMissingError) -> ProblemError:
    return ProblemError(
        BRANCH_MISSING,
        f"The {error.side} branch {error.name!r} of the Proposal no longer exists; close it.",
        extensions={"side": error.side, "branch": error.name},
    )


def _invalid(pointer: str, message: str) -> ProblemError:
    return ProblemError(
        VALIDATION_FAILED,
        "One or more request members are invalid.",
        extensions={"errors": [{"pointer": pointer, "message": message, "code": "invalid"}]},
    )


async def _proposal(
    session: AsyncSession, domain_id: str, proposal_id: str, *, for_update: bool = False
) -> ProposalRow:
    found = await ProposalRepository(session, domain_id).get(proposal_id, for_update=for_update)
    if found is None:
        raise _not_found(proposal_id)
    return found


async def _view(
    session: AsyncSession,
    domain: DomainRow,
    proposal: ProposalRow,
    settings: ProposalSettings,
    *,
    runtime: StoreRuntime | None = None,
    stewards: Stewards | None = None,
) -> ProposalView:
    reviews = await ReviewRepository(session, domain.id).of(proposal.id)
    target = await RefRepository(session, domain.id).get(proposal.target_ref)
    approval = approval_of(
        proposal,
        reviews,
        protected=target is not None and target.protected,
        required=required_approvals(domain, settings),
        settings=settings,
    )
    rule = await steward_approval(session, domain, proposal, approval, settings, runtime, stewards)
    if rule is not None:
        approval = replace(approval, required=max(approval.required, rule.required), stewards=rule)
    approval = replace(
        approval,
        roles=await role_approval(session, domain, proposal, approval, settings, runtime, stewards),
    )
    attempt = await MergeAttemptRepository(session, domain.id).latest(proposal.id)
    return ProposalView(proposal, reviews, approval, attempt)


def _service(
    store: StoreAccess, session: AsyncSession, domain_id: str, settings: ProposalSettings
) -> MergeService:
    return MergeService(
        session,
        domain_id,
        runtime=store.runtime,
        clock=store.clock,
        retention=settings.attempt_retention,
    )


def _request(proposal: ProposalRow, actor: str, **extra: Any) -> MergeRequest:
    return MergeRequest(proposal=proposal, actor=actor, policy=proposal_policy(proposal), **extra)


# Use cases ---------------------------------------------------------------------------------


async def create(
    store: StoreAccess,
    domain_id: str,
    new: NewProposal,
    principal: Principal,
    settings: ProposalSettings,
    *,
    idempotency_key: str | None,
    reconciliation_check: str | None = None,
    stewards: Stewards | None = None,
) -> CreatedProposal:
    """Open a Proposal to merge `source_ref` into `target_ref`, with its merge preview.

    With an idempotency key, a retried create returns the Proposal the same principal created
    under that key (`replayed`); a key reused for other branches is a conflict.
    """
    if new.source_ref == new.target_ref:
        raise _invalid("/body/target_ref", "must differ from source_ref")
    try:
        async with store.sessions() as session, session.begin():
            domain = await require_domain(session, domain_id)
            proposals = ProposalRepository(session, domain_id)
            if idempotency_key is not None:
                existing = await proposals.by_key(principal.id, idempotency_key)
                if existing is not None:
                    return await _replay(
                        session, domain, existing, new, settings, store.runtime, stewards
                    )
            await require_branch(session, domain_id, new.source_ref)
            await require_branch(session, domain_id, new.target_ref)
            duplicates = await proposals.page(
                ProposalFilter(
                    status=ProposalStatus.OPEN,
                    source_ref=new.source_ref,
                    target_ref=new.target_ref,
                ),
                limit=1,
            )
            if duplicates:
                raise ProblemError(
                    ALREADY_EXISTS,
                    f"Proposal {duplicates[0].id!r} already proposes {new.source_ref!r} into "
                    f"{new.target_ref!r}.",
                    extensions={"proposal": duplicates[0].id},
                )
            policy = domain_policy(domain)
            now = store.clock()
            row = ProposalRow(
                id=f"{PROPOSAL_PREFIX}_{new_ulid()}",
                title=new.title,
                description=new.description,
                source_ref=new.source_ref,
                target_ref=new.target_ref,
                origin=new.origin,
                status=ProposalStatus.OPEN,
                author=principal.id,
                created_at=now,
                checks={},
                policies={POLICY_KEY: policy.model_dump(mode="json", exclude_defaults=True)},
                idempotency_key=idempotency_key,
                updated_at=now,
            )
            await proposals.insert(row)
            if settings.preview_on_create:
                await _service(store, session, domain_id, settings).recompute(
                    _request(row, principal.id, reconciliation_check=reconciliation_check)
                )
            created = await _proposal(session, domain_id, row.id)
            return CreatedProposal(
                await _view(
                    session, domain, created, settings, runtime=store.runtime, stewards=stewards
                )
            )
    except IntegrityError:
        if idempotency_key is None:
            raise
        async with store.sessions() as session, session.begin():
            domain = await require_domain(session, domain_id)
            existing = await ProposalRepository(session, domain_id).by_key(
                principal.id, idempotency_key
            )
            if existing is None:
                raise
            return await _replay(session, domain, existing, new, settings, store.runtime, stewards)


async def _replay(
    session: AsyncSession,
    domain: DomainRow,
    existing: ProposalRow,
    new: NewProposal,
    settings: ProposalSettings,
    runtime: StoreRuntime | None = None,
    stewards: Stewards | None = None,
) -> CreatedProposal:
    if (existing.source_ref, existing.target_ref) != (new.source_ref, new.target_ref):
        raise ProblemError(
            IDEMPOTENCY_CONFLICT,
            "The idempotency key was used for another Proposal.",
            extensions={"proposal": existing.id},
        )
    return CreatedProposal(
        await _view(session, domain, existing, settings, runtime=runtime, stewards=stewards),
        replayed=True,
    )


async def get(
    store: StoreAccess,
    domain_id: str,
    proposal_id: str,
    settings: ProposalSettings,
    *,
    stewards: Stewards | None = None,
) -> ProposalView:
    async with store.sessions() as session, session.begin():
        domain = await require_domain(session, domain_id)
        return await _view(
            session,
            domain,
            await _proposal(session, domain_id, proposal_id),
            settings,
            runtime=store.runtime,
            stewards=stewards,
        )


async def page(
    store: StoreAccess,
    domain_id: str,
    filters: ProposalFilter,
    *,
    limit: int,
    after: tuple[datetime, str] | None,
) -> list[ProposalRow]:
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        return await ProposalRepository(session, domain_id).page(filters, limit=limit, after=after)


async def target_of(store: StoreAccess, domain_id: str, proposal_id: str) -> ProposalRow:
    """The Proposal (for authorization against its branches); 404 when unknown."""
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        return await _proposal(session, domain_id, proposal_id)


async def update(
    store: StoreAccess,
    domain_id: str,
    proposal_id: str,
    settings: ProposalSettings,
    *,
    title: str | None,
    description: str | None,
    stewards: Stewards | None = None,
) -> ProposalView:
    """Change the title or description of an open Proposal."""
    async with store.sessions() as session, session.begin():
        domain = await require_domain(session, domain_id)
        proposal = await _proposal(session, domain_id, proposal_id, for_update=True)
        if proposal.status is not ProposalStatus.OPEN:
            raise _not_open(proposal)
        values: dict[str, Any] = {}
        if title is not None:
            values["title"] = title
        if description is not None:
            values["description"] = description
        if values:
            updated = await ProposalRepository(session, domain_id).update(
                proposal_id, at=store.clock(), **values
            )
            assert updated is not None
            proposal = updated
        return await _view(
            session, domain, proposal, settings, runtime=store.runtime, stewards=stewards
        )


async def close(
    store: StoreAccess,
    domain_id: str,
    proposal_id: str,
    settings: ProposalSettings,
    *,
    stewards: Stewards | None = None,
) -> ProposalView:
    """Close an open Proposal; its merge attempts are abandoned. Closing twice is a no-op."""
    async with store.sessions() as session, session.begin():
        domain = await require_domain(session, domain_id)
        proposal = await _proposal(session, domain_id, proposal_id, for_update=True)
        if proposal.status is ProposalStatus.MERGED:
            raise _not_open(proposal)
        if proposal.status is ProposalStatus.OPEN:
            await _service(store, session, domain_id, settings).abandon(proposal_id)
            closed = await ProposalRepository(session, domain_id).update(
                proposal_id, at=store.clock(), status=ProposalStatus.CLOSED
            )
            assert closed is not None
            proposal = closed
        return await _view(
            session, domain, proposal, settings, runtime=store.runtime, stewards=stewards
        )


async def review(
    store: StoreAccess,
    domain_id: str,
    proposal_id: str,
    principal: Principal,
    settings: ProposalSettings,
    *,
    verdict: ReviewVerdict,
    comment: str | None,
    stewards: Stewards | None = None,
) -> tuple[ReviewRow, ProposalView]:
    """Record a review of an open Proposal."""
    async with store.sessions() as session, session.begin():
        domain = await require_domain(session, domain_id)
        proposal = await _proposal(session, domain_id, proposal_id)
        if proposal.status is not ProposalStatus.OPEN:
            raise _not_open(proposal)
        target = await RefRepository(session, domain_id).get(proposal.target_ref)
        if (
            verdict is ReviewVerdict.APPROVE
            and principal.id == proposal.author
            and not settings.allow_self_approval
            and target is not None
            and target.protected
        ):
            raise ProblemError(
                SELF_APPROVAL,
                f"Authors cannot approve their own Proposals into the protected branch "
                f"{proposal.target_ref!r}.",
            )
        row = ReviewRow(
            id=f"{REVIEW_PREFIX}_{new_ulid()}",
            proposal_id=proposal_id,
            reviewer=principal.id,
            verdict=verdict,
            comment=comment,
            at=store.clock(),
        )
        await ReviewRepository(session, domain_id).insert(row)
        return row, await _view(
            session, domain, proposal, settings, runtime=store.runtime, stewards=stewards
        )


async def merge(
    store: StoreAccess,
    domain_id: str,
    proposal_id: str,
    principal: Principal,
    settings: ProposalSettings,
    *,
    idempotency_key: str | None,
    message: str | None,
    page_limit: int,
    scope: ConflictScope = FULL_SCOPE,
    schema_guard: SchemaGuard | None = None,
    reconciliation_check: str | None = None,
    stewards: Stewards | None = None,
) -> MergeOutcome:
    """Run (or re-run) the merge of an open Proposal and commit it when no Conflict remains.

    When the merge would remove sensitivity tags, `schema_guard(target schema, merged
    schema)` decides (it raises to refuse); without one such merges are refused.

    A retried merge with the key of the merge that committed returns that merge (`replayed`).
    The returned attempt lists the Conflicts `scope` shows.
    """
    async with store.sessions() as session, session.begin():
        domain = await require_domain(session, domain_id)
        proposal = await _proposal(session, domain_id, proposal_id, for_update=True)
        attempts = MergeAttemptRepository(session, domain_id)
        if proposal.status is ProposalStatus.MERGED and idempotency_key is not None:
            done = await attempts.by_key(proposal.id, idempotency_key)
            if done is not None and done.status is MergeAttemptStatus.MERGED:
                view = await _attempt_view(
                    session,
                    domain_id,
                    proposal,
                    done,
                    ConflictQuery(limit=page_limit),
                    partitions=await _partitions(store, session, domain_id, done, scope),
                )
                return MergeOutcome(proposal, None, view, head=done.revision, replayed=True)
        if proposal.status is not ProposalStatus.OPEN:
            raise _not_open(proposal)
        view_now = await _view(
            session, domain, proposal, settings, runtime=store.runtime, stewards=stewards
        )
        approval = view_now.approval
        if (
            not approval.satisfied
            and approval.stewards is not None
            and (approval.stewards.unavailable)
        ):
            rule = approval.stewards
            raise ProblemError(
                STEWARDS_UNAVAILABLE,
                f"Merging into {proposal.target_ref!r} needs {rule.required} approval(s) from the "
                f"stewards of this Proposal ({', '.join(rule.roles)}), but only "
                f"{rule.assignees} principal(s) can give them.",
                extensions={
                    "required_from_stewards": rule.required,
                    "steward_roles": list(rule.roles),
                    "assignees": rule.assignees,
                },
            )
        if not approval.satisfied:
            raise ProblemError(
                APPROVALS_REQUIRED,
                f"Merging into the protected branch {proposal.target_ref!r} needs "
                f"{approval.required} approval(s) and no outstanding request for changes"
                + (
                    f", and an approval from each of the roles {', '.join(approval.roles.missing)}."
                    if approval.roles is not None and approval.roles.missing
                    else "."
                ),
                extensions={
                    "required": approval.required,
                    "approvals": len(approval.approvers),
                    "approvers": list(approval.approvers),
                    "changes_requested": list(approval.changes_requested),
                    **(
                        {}
                        if approval.stewards is None
                        else {
                            "required_from_stewards": approval.stewards.required,
                            "steward_approvals": len(approval.stewards.approvers),
                        }
                    ),
                    **(
                        {}
                        if approval.roles is None
                        else {
                            "required_roles": list(approval.roles.roles),
                            "missing_roles": list(approval.roles.missing),
                        }
                    ),
                },
            )
        refs = RefRepository(session, domain_id)
        source = await refs.get(proposal.source_ref)
        delete_source = (
            settings.auto_close_source_branch
            and source is not None
            and not source.protected
            and source.name != domain.default_branch
        )
        try:
            result = await _service(store, session, domain_id, settings).merge(
                _request(
                    proposal,
                    principal.id,
                    message=message,
                    idempotency_key=idempotency_key,
                    delete_source=delete_source,
                    schema_guard=schema_guard,
                    reconciliation_check=reconciliation_check,
                )
            )
        except ProposalBranchMissingError as error:
            raise _branch_missing(error) from None
        except ResolutionError as error:
            raise _invalid("/body", str(error)) from None
        if result.reconciliation_blocked:
            checks = (await _proposal(session, domain_id, proposal_id)).checks or {}
            found = dict(checks.get("reconciliation") or {})
            raise ProblemError(
                RECONCILIATION_OPEN,
                f"{found.get('blocking', 0)} open contradiction(s) between Sources are raised by "
                "the changes of this Proposal; decide them in the reconciliation queue first.",
                extensions={"candidates": list(found.get("candidates") or [])},
            )
        if result.head is not None:
            await settle_merged(
                session, domain_id, proposal_id, revision=result.head, at=store.clock()
            )
        updated = await _proposal(session, domain_id, proposal_id)
        query = ConflictQuery(resolved=None if result.merged else False, limit=page_limit)
        view = await _attempt_view(
            session,
            domain_id,
            updated,
            result.attempt,
            query,
            recomputed=True,
            stale=result.stale_previous,
            partitions=await _partitions(store, session, domain_id, result.attempt, scope),
        )
        return MergeOutcome(
            updated,
            result,
            view,
            head=result.head,
            source_deleted=result.source_deleted,
        )


async def conflicts(
    store: StoreAccess,
    domain_id: str,
    proposal_id: str,
    principal: Principal,
    settings: ProposalSettings,
    query: ConflictQuery,
    *,
    reconciliation_check: str | None = None,
) -> AttemptView:
    """The current attempt of a Proposal with a page of its Conflicts.

    For an open Proposal the attempt is recomputed when there is none or a head moved since
    (resolutions whose Conflict disappeared are reported as unmatched).
    """
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        proposal = await _proposal(session, domain_id, proposal_id)
        if proposal.status is ProposalStatus.OPEN:
            try:
                result = await _service(store, session, domain_id, settings).current(
                    _request(proposal, principal.id, reconciliation_check=reconciliation_check)
                )
            except ProposalBranchMissingError as error:
                raise _branch_missing(error) from None
            proposal = await _proposal(session, domain_id, proposal_id)
            return await _attempt_view(
                session,
                domain_id,
                proposal,
                result.attempt,
                query,
                recomputed=result.computation is not None,
                stale=result.stale_previous,
                partitions=await _partitions(
                    store, session, domain_id, result.attempt, query.scope
                ),
            )
        latest = await MergeAttemptRepository(session, domain_id).latest(proposal_id)
        if latest is None:
            raise ProblemError(NOT_FOUND, f"Proposal {proposal_id!r} has no merge attempt.")
        return await _attempt_view(
            session,
            domain_id,
            proposal,
            latest,
            query,
            partitions=await _partitions(store, session, domain_id, latest, query.scope),
        )


async def _partitions(
    store: StoreAccess,
    session: AsyncSession,
    domain_id: str,
    attempt: MergeAttemptRow,
    scope: ConflictScope,
) -> PartitionFilter:
    """The partitions whose Conflicts a reader limited to `scope` sees, by the schemas of the
    attempt's Revisions."""
    if scope.everything:
        return EVERY_PARTITION
    revisions = [r for r in (attempt.base, attempt.ours, attempt.theirs) if r is not None]
    schemas = [
        await schema_at(
            session, domain_id, await require_revision(session, domain_id, r), store.runtime
        )
        for r in dict.fromkeys(revisions)
    ]
    return visible_partitions(scope, schemas)


async def resolve(
    store: StoreAccess,
    domain_id: str,
    proposal_id: str,
    principal: Principal,
    settings: ProposalSettings,
    inputs: Sequence[ResolutionInput],
    *,
    rerun: bool,
    page_limit: int,
    scope: ConflictScope = FULL_SCOPE,
    reconciliation_check: str | None = None,
) -> tuple[list[ResolutionRow], AttemptView | None]:
    """Store resolutions of Conflicts of the current attempt; with `rerun`, merge again.

    Every resolution must name a Conflict of the current attempt (recomputed first when a
    head moved, 404 otherwise) that `scope` shows (403 otherwise) and fit it (422
    otherwise); on any error nothing is stored. The re-run attempt lists the Conflicts
    `scope` shows.
    """
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        proposal = await _proposal(session, domain_id, proposal_id, for_update=True)
        if proposal.status is not ProposalStatus.OPEN:
            raise _not_open(proposal)
        service = _service(store, session, domain_id, settings)
        try:
            current = await service.current(
                _request(proposal, principal.id, reconciliation_check=reconciliation_check)
            )
        except ProposalBranchMissingError as error:
            raise _branch_missing(error) from None
        partitions = await _partitions(store, session, domain_id, current.attempt, scope)
        conflicts_repo = ConflictRepository(session, domain_id)
        now = store.clock()
        rows: list[ResolutionRow] = []
        for item in inputs:
            stored = await conflicts_repo.get(current.attempt.id, item.fingerprint)
            if stored is None:
                raise unknown_conflict(item.fingerprint, current.attempt.id)
            require_visible(partitions, stored, current.attempt.id, invisible=scope.invisible)
            resolution = _resolution(item, principal.id)
            payload = {k: v for k, v in stored.payload.items() if k != "members"}
            try:
                check_resolution(
                    Conflict.model_validate(payload),
                    tuple(stored.payload.get("members") or ()),
                    resolution,
                )
            except ResolutionError as error:
                raise _invalid(item.pointer, str(error)) from None
            rows.append(
                ResolutionRow(
                    fingerprint=item.fingerprint,
                    resolution=resolution.model_dump(mode="json", exclude={"resolver"}),
                    resolved_by=principal.id,
                    at=now,
                )
            )
        await ResolutionRepository(session, domain_id).upsert_many(proposal_id, rows)
        if not rerun:
            return rows, None
        try:
            result = await service.recompute(
                _request(proposal, principal.id, reconciliation_check=reconciliation_check)
            )
        except ResolutionError as error:
            raise _invalid("/body", str(error)) from None
        proposal = await _proposal(session, domain_id, proposal_id)
        view = await _attempt_view(
            session,
            domain_id,
            proposal,
            result.attempt,
            ConflictQuery(limit=page_limit),
            recomputed=True,
            stale=result.stale_previous,
            partitions=await _partitions(store, session, domain_id, result.attempt, scope),
        )
        return rows, view


def _resolution(item: ResolutionInput, resolver: str) -> Resolution:
    try:
        return Resolution.model_validate(
            {
                "fingerprint": item.fingerprint,
                "choice": item.choice,
                "value": item.value,
                "values": item.values,
                "resolver": resolver,
            }
        )
    except ValidationError as error:
        raise _invalid(item.pointer, error.errors()[0]["msg"]) from None


async def _attempt_view(
    session: AsyncSession,
    domain_id: str,
    proposal: ProposalRow,
    attempt: MergeAttemptRow,
    query: ConflictQuery,
    *,
    recomputed: bool = False,
    stale: bool = False,
    partitions: PartitionFilter = EVERY_PARTITION,
) -> AttemptView:
    """One page of the Conflicts of `attempt` in `partitions`; a reader limited to some
    partitions also sees only the resolutions and automatic resolutions of those."""
    repository = ConflictRepository(session, domain_id)
    rows = await repository.page(
        attempt.id,
        codes=query.codes,
        resolved=query.resolved,
        limit=query.limit + 1,
        after=query.after,
        partitions=partitions,
    )
    more = len(rows) > query.limit
    rows = rows[: query.limit]
    resolutions = await ResolutionRepository(session, domain_id).of(proposal.id)
    if not partitions.everything:
        visible = await repository.fingerprints(attempt.id, partitions)
        resolutions = [r for r in resolutions if r.fingerprint in visible]
        attempt = _limited(attempt, partitions)
    return AttemptView(
        proposal=proposal,
        attempt=attempt,
        conflicts=rows,
        next_after=rows[-1].fingerprint if more and rows else None,
        counts=await repository.counts(attempt.id, partitions),
        resolutions=resolutions,
        recomputed=recomputed,
        stale=stale,
    )


async def scoped(
    store: StoreAccess, domain_id: str, view: ProposalView, scope: ConflictScope
) -> ProposalView:
    """`view` with its attempt summary limited to the Conflicts `scope` shows."""
    if scope.everything or view.attempt is None:
        return view
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        partitions = await _partitions(store, session, domain_id, view.attempt, scope)
        counts = await ConflictRepository(session, domain_id).counts(view.attempt.id, partitions)
    return replace(view, attempt=limited_summary(view.attempt, partitions, counts))


def limited_summary(
    attempt: MergeAttemptRow, partitions: PartitionFilter, counts: dict[tuple[str, bool], int]
) -> MergeAttemptRow:
    """`attempt` limited to `partitions` (see `_limited`), its open and resolved totals taken
    from `counts`, the per-code counts of those partitions."""
    limited = _limited(attempt, partitions)
    outcome = dict(limited.outcome or {})
    outcome["open"] = sum(n for (_, resolved), n in counts.items() if not resolved)
    outcome["resolved"] = sum(n for (_, resolved), n in counts.items() if resolved)
    return replace(limited, outcome=outcome)


def _limited(attempt: MergeAttemptRow, partitions: PartitionFilter) -> MergeAttemptRow:
    """`attempt` with the outcome members a reader limited to `partitions` may see: automatic
    resolutions in those partitions, and no unmatched resolutions (their Conflict, and so
    its partition, is gone)."""
    outcome = dict(attempt.outcome or {})
    outcome["auto_resolutions"] = [
        a
        for a in outcome.get("auto_resolutions") or []
        if isinstance(a, dict)
        and partitions.allows(
            str(a.get("partition")),
            None if a.get("attribute") is None else str(a.get("attribute")),
        )
    ]
    outcome["unmatched_resolutions"] = []
    return replace(attempt, outcome=outcome)
