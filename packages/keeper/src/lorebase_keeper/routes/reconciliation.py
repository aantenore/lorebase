"""The reconciliation queue (design 10.6, 11.1): contradictions between Sources.

| Method and path | Permission | Notes |
|-----------------|------------|-------|
| `GET .../reconciliation` | `data.read` (branch) | filters, see `list_candidates` |
| `GET .../reconciliation/{id}` | `data.read` (its branch) | |
| `POST .../reconciliation/{id}/resolution` | `conflict.resolve`, and the write's own | `dry_run` |
| `POST .../reconciliation/{id}/dismissal` | `conflict.resolve` | reason required |
| `POST .../reconciliation/{id}/reopen` | `conflict.resolve` | dismissed only |
| `POST .../reconciliation:resolve` | `conflict.resolve` | a policy or a dismissal, in bulk |
| `POST .../reconciliation/sweeps` | `job.manage` | 202, queues `reconcile_sweep` |

Reads are scoped like Record reads: `data.read` on the branch shows every candidate of it,
grants narrowed to Kinds show the candidates of those Kinds only; claim values, evidence
quotes and decided values are masked by clearance (`services.reconciliation`). Decisions need
`conflict.resolve` on the candidate's branch, plus what the write itself needs: `update_{kind}`
needs `operation.execute`, a write needing review `branch.create` and `proposal.create` on
`reconcile/{candidate}-{random}`. Every decision is in the audit log (`reconciliation.resolve`,
`reconciliation.dismiss`, `reconciliation.reopen`), written with the decision.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Schema
from lorebase_core.reconcile import CandidateStatus, CandidateType
from lorebase_keeper.auth.audit import annotate, suppress
from lorebase_keeper.auth.deps import AuthDep, PrincipalDep, authorize
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.rbac import AccessContext
from lorebase_keeper.context import StoreDep, require_branch, require_domain
from lorebase_keeper.deps import KeeperSettingsDep
from lorebase_keeper.errors import VALIDATION_FAILED, Problem, ProblemError, ValidationProblem
from lorebase_keeper.pagination import MAX_CURSOR_LENGTH, Page, decode_cursor, encode_cursor
from lorebase_keeper.reading.points import read_guard
from lorebase_keeper.reading.sensitivity import Masking, default_schema, masking_for
from lorebase_keeper.services import reconciliation as service
from lorebase_keeper.services.errors import BRANCH_BUSY, commit_problem
from lorebase_keeper.services.reads import ReadGuard
from lorebase_keeper.services.reconciliation_settle import SWEEP_ACTION
from lorebase_keeper.settings import KeeperSettings
from lorebase_store.commit import CommitError
from lorebase_store.jobs import RECONCILE_SWEEP_JOB, enqueue_job, reconcile_sweep_payload
from lorebase_store.reads import head_schema
from lorebase_store.repositories import (
    CandidateFilter,
    CandidateRow,
    LockTimeoutError,
    ReconciliationCandidateRepository,
)

router = APIRouter(tags=["reconciliation"])

QUEUE = "/domains/{domain_id}/reconciliation"
CANDIDATE = QUEUE + "/{candidate_id}"
DataRead = Annotated[ReadGuard, Depends(read_guard())]

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "Missing permission or clearance."},
    404: {"model": Problem, "description": "Domain, branch or candidate not found."},
    409: {
        "model": Problem,
        "description": "The candidate is not open (`candidate-not-open`), or the write needs "
        "review and its Proposal cannot be opened.",
    },
    422: {"model": ValidationProblem, "description": "Invalid request, or the value is invalid."},
    503: {"model": Problem, "description": "The branch is locked by another writer."},
}


# Models -----------------------------------------------------------------------------------


class ReconciliationClaimOut(BaseModel):
    """One value and where it comes from (design 10.6)."""

    model_config = ConfigDict(extra="allow")

    claim_id: str
    value: JsonValue = Field(description="Masked when the caller is not cleared for it.")
    value_hash: str | None = None
    source: str | None = Field(
        default=None, description="Source identity: `source:`, `document:`, `principal:`..."
    )
    provenance_id: str | None = None
    proposal_item: dict[str, str] | None = None
    evidence: dict[str, JsonValue] | None = Field(
        default=None, description="Document, chunk, offsets and quote of an extracted value."
    )
    valid_from: str | None = None
    valid_to: str | None = None
    at: str | None = None


class ReconciliationCandidateOut(BaseModel):
    """A reconciliation candidate and its claims."""

    id: str
    branch: str
    type: CandidateType
    status: CandidateStatus
    kind: str | None = Field(description="Kind api name (null when the schema lost it).")
    kind_id: str
    keys: list[str]
    attribute: str | None = Field(description="Attribute api name; null for duplicates.")
    attribute_id: str | None
    claims: list[ReconciliationClaimOut]
    claim_count: int = Field(description="Every distinct claim seen; the newest are listed.")
    signals: dict[str, JsonValue]
    proposal_id: str | None = Field(
        description="The Proposal that raised the candidate, or that a decision opened."
    )
    decision: dict[str, Any] | None = Field(
        description="The last decision: choice, claim, policy, reason and the claims then."
    )
    decided_by: str | None
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ReconciliationResolveIn(BaseModel):
    """A decision (design 10.6): `claim` (`claim_id`), `keep_current`, `custom` (`value`),
    `policy` (`policy`) or, for duplicates, `merge` (`survivor`, `values`)."""

    model_config = ConfigDict(extra="forbid")

    choice: service.ReconciliationChoice
    claim_id: str | None = None
    value: JsonValue = None
    policy: service.ReconciliationPolicy | None = None
    survivor: str | None = Field(default=None, description="Key of the Record that stays.")
    values: dict[str, JsonValue] | None = Field(
        default=None, description="Values to set on the survivor, by Attribute api name."
    )
    reason: str = Field(min_length=1, max_length=2000)
    dry_run: bool = False


class ReconciliationReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)


class ReconciliationDecisionOut(BaseModel):
    """Where a decision left the candidate."""

    status: Literal["resolved", "proposed", "undecided", "dry_run", "dismissed", "reopened"]
    candidate: ReconciliationCandidateOut
    claim_id: str | None = Field(default=None, description="The claim chosen.")
    revision: str | None = Field(default=None, description="Revision the write committed.")
    proposal: str | None = Field(
        default=None, description="Proposal of a write that needs review (`proposed`)."
    )
    branch: str | None = Field(default=None, description="Branch written.")
    edits: list[dict[str, Any]] = Field(default_factory=list)
    detail: str | None = None


class ReconciliationFilterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch: str | None = None
    type: CandidateType | None = None
    kind: str | None = None
    attribute: str | None = None
    source: str | None = None


class ReconciliationBulkIn(BaseModel):
    """One choice over open candidates: the `ids` given, or those matching `filter`."""

    model_config = ConfigDict(extra="forbid")

    ids: list[str] | None = Field(default=None, min_length=1)
    filter: ReconciliationFilterIn | None = None
    choice: Literal["policy", "dismiss"]
    policy: service.ReconciliationPolicy | None = None
    reason: str = Field(min_length=1, max_length=2000)
    dry_run: bool = False


class ReconciliationBulkItemOut(BaseModel):
    candidate: str
    status: str = Field(description="As a single decision, or `failed`.")
    detail: str | None = None
    proposal: str | None = None
    revision: str | None = None


class ReconciliationBulkOut(BaseModel):
    count: int
    counts: dict[str, int] = Field(description="Candidates per status.")
    items: list[ReconciliationBulkItemOut]


class ReconciliationSweepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch: str | None = Field(default=None, description="Default: the default branch.")


class ReconciliationSweepOut(BaseModel):
    job_id: int
    branch: str


# Helpers ----------------------------------------------------------------------------------


def _invalid(pointer: str, message: str) -> ProblemError:
    return ProblemError(
        VALIDATION_FAILED,
        "One or more request members are invalid.",
        extensions={"errors": [{"pointer": pointer, "message": message, "code": "invalid"}]},
    )


def _context(
    request: Request,
    store: StoreDep,
    domain_id: str,
    principal: PrincipalDep,
    auth: AuthDep,
    keeper: KeeperSettings,
) -> service.Context:
    async def check(permission: Permission, context: AccessContext) -> None:
        await authorize(request, auth, principal, permission, domain_id, context)

    async def reveal(session: AsyncSession) -> Masking:
        reference = await default_schema(session, domain_id, store.runtime)
        return await masking_for(
            auth.authorizer, principal, domain_id, reference, keeper.reads.sensitivity
        )

    return service.Context(
        store=store,
        domain_id=domain_id,
        principal=principal,
        keeper=keeper,
        check=check,
        reveal=reveal,
        proposals_path=f"{keeper.api_prefix}/domains/{domain_id}/proposals",
    )


async def _kinds(guard: ReadGuard, branch: str) -> frozenset[str] | None:
    """Kind api names the caller may read on `branch`; None for every Kind (403 for none)."""
    if await guard.may(branch):
        return None
    kinds = await guard.granted_kinds()
    if not kinds:
        raise guard.denied()
    return kinds


def _kind_ids(schema: Schema | None, names: frozenset[str] | None) -> frozenset[str] | None:
    if names is None:
        return None
    if schema is None:
        return frozenset()
    return frozenset(k.id for k in schema.kinds.values() if k.api_name in names)


async def _readable(guard: ReadGuard, ctx: service.Context, candidate: CandidateRow) -> None:
    """403 unless the caller may read `candidate` (its branch, or its Kind)."""
    names = await _kinds(guard, candidate.branch)
    if names is None:
        return
    async with ctx.store.sessions() as session, session.begin():
        head = await head_schema(
            session, ctx.domain_id, candidate.branch, runtime=ctx.store.runtime
        )
    if candidate.kind_id not in (_kind_ids(None if head is None else head.schema, names) or ()):
        raise guard.denied()


async def _render(
    ctx: service.Context, rows: list[CandidateRow]
) -> list[ReconciliationCandidateOut]:
    """`rows` as the caller may see them: masked, vault references opened when cleared."""
    out: list[ReconciliationCandidateOut] = []
    async with ctx.store.sessions() as session, session.begin():
        await require_domain(session, ctx.domain_id)
        base = await ctx.reveal(session)
        schemas: dict[str, Schema | None] = {}
        for row in rows:
            if row.branch not in schemas:
                head = await head_schema(
                    session, ctx.domain_id, row.branch, runtime=ctx.store.runtime
                )
                schemas[row.branch] = None if head is None else head.schema
            schema = schemas[row.branch]
            masking = base if schema is None else base.over(schema)
            seen = service.shown(row, schema, masking)
            kind = None if schema is None else schema.kinds.get(row.kind_id)
            attribute = (
                None
                if kind is None or row.attribute_id is None
                else kind.attributes.get(row.attribute_id)
            )
            body: dict[str, Any] = {
                "id": row.id,
                "branch": row.branch,
                "type": row.type,
                "status": row.status,
                "kind": None if kind is None else kind.api_name,
                "kind_id": row.kind_id,
                "keys": list(row.keys),
                "attribute": None if attribute is None else attribute.api_name,
                "attribute_id": row.attribute_id,
                "claims": seen["claims"],
                "claim_count": row.claim_count,
                "signals": dict(row.signals or {}),
                "proposal_id": row.proposal_id,
                "decision": seen["decision"],
                "decided_by": row.decided_by,
                "decided_at": row.decided_at,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            data: Any = {"claims": body["claims"], "decision": body["decision"]}
            await service.disclose(session, ctx, () if schema is None else (schema,), masking, data)
            body.update(data)
            out.append(ReconciliationCandidateOut.model_validate(body))
    return out


def _decision(
    outcome: service.DecisionOutcome, candidate: ReconciliationCandidateOut
) -> ReconciliationDecisionOut:
    return ReconciliationDecisionOut(
        status=outcome.status.value,
        candidate=candidate,
        claim_id=outcome.claim_id,
        revision=outcome.revision,
        proposal=outcome.proposal,
        branch=outcome.branch,
        edits=outcome.edits,
        detail=outcome.detail,
    )


def _limit(keeper: KeeperSettings, limit: int | None) -> int:
    bounds = keeper.reconciliation
    effective = bounds.default_limit if limit is None else limit
    if effective > bounds.max_limit:
        raise _invalid("/query/limit", f"must not exceed {bounds.max_limit}")
    return effective


def _after(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        position = decode_cursor(cursor)
        return datetime.fromisoformat(str(position["at"])), str(position["id"])
    except (KeyError, ValueError):
        raise _invalid("/query/cursor", "is not a valid cursor") from None


async def _filter(
    ctx: service.Context,
    guard: ReadGuard,
    *,
    branch: str | None,
    type_: CandidateType | None,
    status: CandidateStatus | None,
    kind: str | None,
    attribute: str | None,
    source: str | None,
) -> CandidateFilter:
    """A listing filter on `branch` (default: the default branch), Kinds and Attributes named
    by api name, narrowed to the Kinds the caller may read."""
    await guard.precheck()
    async with ctx.store.sessions() as session, session.begin():
        domain = await require_domain(session, ctx.domain_id)
        name = branch or domain.default_branch
        await require_branch(session, ctx.domain_id, name)
        head = await head_schema(session, ctx.domain_id, name, runtime=ctx.store.runtime)
    schema = None if head is None else head.schema
    kind_id = attribute_id = None
    if kind is not None:
        found = None if schema is None else schema.kind_by_api_name(kind)
        if found is None:
            raise _invalid("/query/kind", f"no Kind {kind!r} on branch {name!r}")
        kind_id = found.id
        if attribute is not None:
            named = found.attribute_by_api_name(attribute)
            if named is None:
                raise _invalid("/query/attribute", f"no Attribute {attribute!r} of {kind}")
            attribute_id = named.id
    elif attribute is not None:
        raise _invalid("/query/attribute", "needs kind")
    return CandidateFilter(
        branch=name,
        type=type_,
        status=status,
        kind_id=kind_id,
        attribute_id=attribute_id,
        source=source,
        kind_ids=_kind_ids(schema, await _kinds(guard, name)),
    )


def _failure(error: Exception, keeper: KeeperSettings) -> ProblemError:
    if isinstance(error, LockTimeoutError):
        return ProblemError(
            BRANCH_BUSY,
            "Another writer holds the branch; retry later.",
            headers={"Retry-After": str(keeper.branches.lock_retry_after_s)},
        )
    assert isinstance(error, CommitError)
    return commit_problem(
        error,
        max_issues=keeper.operations.max_issues,
        retry_after_s=keeper.branches.lock_retry_after_s,
    )


async def _decidable(
    request: Request,
    auth: AuthDep,
    principal: PrincipalDep,
    guard: ReadGuard,
    ctx: service.Context,
    candidate_id: str,
) -> CandidateRow:
    """The candidate, after `conflict.resolve` on its branch and read access to it."""
    await guard.precheck()
    candidate = await service.get(ctx, candidate_id)
    await authorize(
        request,
        auth,
        principal,
        Permission.CONFLICT_RESOLVE,
        ctx.domain_id,
        AccessContext(branch=candidate.branch),
    )
    await _readable(guard, ctx, candidate)
    return candidate


# Routes -----------------------------------------------------------------------------------


@router.get(QUEUE, summary="List reconciliation candidates", responses=_ERRORS)
async def list_candidates(
    domain_id: str,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    guard: DataRead,
    branch: Annotated[str | None, Query(description="Default: the default branch.")] = None,
    type: CandidateType | None = None,
    status: CandidateStatus | None = None,
    kind: Annotated[str | None, Query(description="Kind api name.")] = None,
    attribute: Annotated[str | None, Query(description="Attribute api name (with kind).")] = None,
    source: Annotated[
        str | None, Query(description="Source identity of a claim, e.g. `source:src_...`.")
    ] = None,
    limit: Annotated[int | None, Query(ge=1, description="Page size.")] = None,
    cursor: Annotated[
        str | None, Query(max_length=MAX_CURSOR_LENGTH, description="Opaque page cursor.")
    ] = None,
) -> Page[ReconciliationCandidateOut]:
    """Candidates of a branch, newest first."""
    ctx = _context(request, store, domain_id, principal, auth, keeper)
    filters = await _filter(
        ctx,
        guard,
        branch=branch,
        type_=type,
        status=status,
        kind=kind,
        attribute=attribute,
        source=source,
    )
    size = _limit(keeper, limit)
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        rows = await ReconciliationCandidateRepository(session, domain_id).page(
            filters, limit=size + 1, after=_after(cursor)
        )
    more = len(rows) > size
    rows = rows[:size]
    next_cursor = (
        encode_cursor({"at": rows[-1].created_at.isoformat(), "id": rows[-1].id})
        if more and rows
        else None
    )
    return Page[ReconciliationCandidateOut](items=await _render(ctx, rows), next_cursor=next_cursor)


@router.get(CANDIDATE, summary="Get a reconciliation candidate", responses=_ERRORS)
async def get_candidate(
    domain_id: str,
    candidate_id: str,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    guard: DataRead,
) -> ReconciliationCandidateOut:
    """One candidate with its claims and last decision."""
    await guard.precheck()
    ctx = _context(request, store, domain_id, principal, auth, keeper)
    candidate = await service.get(ctx, candidate_id)
    await _readable(guard, ctx, candidate)
    return (await _render(ctx, [candidate]))[0]


@router.post(CANDIDATE + "/resolution", summary="Resolve a candidate", responses=_ERRORS)
async def resolve_candidate(
    domain_id: str,
    candidate_id: str,
    body: ReconciliationResolveIn,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    guard: DataRead,
) -> ReconciliationDecisionOut:
    """Decide a candidate: the value is written by `update_{kind}` on its branch (proposed
    from a new branch `reconcile/{candidate}-{random}` when the branch needs review);
    `keep_current` writes nothing. `dry_run` reports the edits without deciding."""
    ctx = _context(request, store, domain_id, principal, auth, keeper)
    await _decidable(request, auth, principal, guard, ctx, candidate_id)
    try:
        outcome = await service.resolve(
            ctx,
            candidate_id,
            service.DecisionInput(
                choice=body.choice,
                reason=body.reason,
                claim_id=body.claim_id,
                value=body.value,
                policy=body.policy,
                survivor=body.survivor,
                values=body.values,
                dry_run=body.dry_run,
            ),
        )
    except (CommitError, LockTimeoutError) as error:
        raise _failure(error, keeper) from None
    annotate(
        request,
        action="reconciliation.dry_run" if body.dry_run else "reconciliation.resolve",
        target=candidate_id,
        status=outcome.status.value,
        choice=body.choice.value,
    )
    if outcome.status in (service.Outcome.RESOLVED, service.Outcome.PROPOSED):
        suppress(request)  # audited with the decision
    return _decision(outcome, (await _render(ctx, [outcome.candidate]))[0])


@router.post(CANDIDATE + "/dismissal", summary="Dismiss a candidate", responses=_ERRORS)
async def dismiss_candidate(
    domain_id: str,
    candidate_id: str,
    body: ReconciliationReasonIn,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    guard: DataRead,
) -> ReconciliationDecisionOut:
    """Dismiss an open candidate; nothing is written. It can be reopened."""
    ctx = _context(request, store, domain_id, principal, auth, keeper)
    await _decidable(request, auth, principal, guard, ctx, candidate_id)
    outcome = await service.dismiss(ctx, candidate_id, body.reason)
    annotate(request, action="reconciliation.dismiss", target=candidate_id)
    suppress(request)
    return _decision(outcome, (await _render(ctx, [outcome.candidate]))[0])


@router.post(CANDIDATE + "/reopen", summary="Reopen a dismissed candidate", responses=_ERRORS)
async def reopen_candidate(
    domain_id: str,
    candidate_id: str,
    body: ReconciliationReasonIn,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    guard: DataRead,
) -> ReconciliationDecisionOut:
    """Open a dismissed candidate again."""
    ctx = _context(request, store, domain_id, principal, auth, keeper)
    await _decidable(request, auth, principal, guard, ctx, candidate_id)
    outcome = await service.reopen(ctx, candidate_id, body.reason)
    annotate(request, action="reconciliation.reopen", target=candidate_id)
    suppress(request)
    return _decision(outcome, (await _render(ctx, [outcome.candidate]))[0])


@router.post(QUEUE + ":resolve", summary="Resolve candidates in bulk", responses=_ERRORS)
async def resolve_candidates(
    domain_id: str,
    body: ReconciliationBulkIn,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    guard: DataRead,
) -> ReconciliationBulkOut:
    """Apply a policy, or dismiss, over at most `keeper.reconciliation.bulk_max` open
    candidates; each is decided and audited on its own, and one that fails does not stop the
    others."""
    if (body.ids is None) == (body.filter is None):
        raise _invalid("/body", "give either ids or filter")
    if body.choice == "policy" and body.policy is None:
        raise _invalid("/body/policy", "required for the choice policy")
    bulk_max = keeper.reconciliation.bulk_max
    ctx = _context(request, store, domain_id, principal, auth, keeper)
    if body.ids is not None:
        if len(body.ids) > bulk_max:
            raise _invalid("/body/ids", f"at most {bulk_max} candidates per request")
        ids = list(dict.fromkeys(body.ids))
        rows = [await service.get(ctx, i) for i in ids]
    else:
        assert body.filter is not None
        filters = await _filter(
            ctx,
            guard,
            branch=body.filter.branch,
            type_=body.filter.type,
            status=CandidateStatus.OPEN,
            kind=body.filter.kind,
            attribute=body.filter.attribute,
            source=body.filter.source,
        )
        async with store.sessions() as session, session.begin():
            await require_domain(session, domain_id)
            rows = await ReconciliationCandidateRepository(session, domain_id).page(
                filters, limit=bulk_max + 1
            )
        if len(rows) > bulk_max:
            raise _invalid("/body/filter", f"matches more than {bulk_max} candidates")
    for branch in sorted({row.branch for row in rows}):
        await authorize(
            request,
            auth,
            principal,
            Permission.CONFLICT_RESOLVE,
            domain_id,
            AccessContext(branch=branch),
        )
    for row in rows:
        await _readable(guard, ctx, row)
    try:
        items = await service.bulk(
            ctx,
            [row.id for row in rows],
            policy=body.policy if body.choice == "policy" else None,
            reason=body.reason,
            dry_run=body.dry_run,
        )
    except (CommitError, LockTimeoutError) as error:
        raise _failure(error, keeper) from None
    counts: dict[str, int] = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    annotate(
        request,
        action="reconciliation.bulk_resolve",
        target=domain_id,
        choice=body.choice,
        policy=None if body.policy is None else body.policy.value,
        count=len(items),
        counts=counts,
        dry_run=body.dry_run,
    )
    return ReconciliationBulkOut(
        count=len(items),
        counts=counts,
        items=[ReconciliationBulkItemOut(**vars(item)) for item in items],
    )


@router.post(
    QUEUE + "/sweeps", status_code=202, summary="Queue a reconciliation sweep", responses=_ERRORS
)
async def queue_sweep(
    domain_id: str,
    request: Request,
    principal: PrincipalDep,
    auth: AuthDep,
    store: StoreDep,
    body: ReconciliationSweepIn | None = None,
) -> ReconciliationSweepOut:
    """Queue a sweep of a branch for Scribe (job `reconcile_sweep`); an identical sweep that
    is queued or running is returned instead of a new one."""
    await authorize(request, auth, principal, Permission.JOB_MANAGE, domain_id)
    async with store.sessions() as session, session.begin():
        domain = await require_domain(session, domain_id)
        branch = (body.branch if body is not None else None) or domain.default_branch
        await require_branch(session, domain_id, branch)
        job_id = await enqueue_job(
            session,
            RECONCILE_SWEEP_JOB,
            domain_id=domain_id,
            payload=reconcile_sweep_payload(branch=branch),
            unique=True,
        )
    annotate(request, action=SWEEP_ACTION, target=branch, job=job_id)
    return ReconciliationSweepOut(job_id=job_id, branch=branch)
