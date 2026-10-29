"""Decisions of the reconciliation queue (design 10.6): resolve, dismiss, reopen, bulk.

A candidate groups claims that disagree; a person decides it. Nothing here decides on its own
(AC-33): a policy is applied only when a person asks for it, and when it cannot tell the claims
apart the candidate stays open.

Choices of a resolution:

- `claim`: the value of one claim;
- `keep_current`: the value on the branch stays; nothing is written;
- `custom`: a value given by the person, validated against the schema like any update;
- `policy`: `authority` (the ranking of 6.10 over source identities, the Attribute's
  `authority` Source first, then the Domain's merge policy rankings) or `newest_wins` (period
  start when a claim has one, else its time), folded pairwise over the claims with
  `decide_authority` and `decide_newest`; `prefer_current` and `prefer_incoming` pick the
  current or the other claim of a two-claim candidate;
- `merge` (duplicates only): the surviving Record, the values to set on it, the Links of the
  absorbed Record re-created on it and the absorbed Record deleted, on branch
  `reconcile/{candidate}-{random}`, proposed into the candidate's branch.

A value is written by the Kind's built-in `update_{kind}` through the Operation pipeline on
the candidate's branch, so authorization, validation and branch protection apply unchanged;
its provenance entry has a `reconciliation` origin (and the chosen claim's period on a
`valid_time` Attribute). The decision is recorded and audited in the transaction of the
write. When the write needs review (protected branch), it is made on a new branch
`reconcile/{candidate}-{random}` (never one that exists, each write expecting the head the
previous one left) and proposed instead: the candidate stays open, linked to that Proposal,
until the merge resolves it (`reconciliation_settle`). `keep_current`, a claim equal to the
current value, dismissals and reopenings write no Revision; they are recorded on the candidate
and in the audit log. Every decision needs a reason. While the Proposal of a pending decision is
open, no other decision is taken (409 `reconciliation-pending`); once it is closed, a new
decision replaces the pending one. When proposing fails after the branch was created, the
Proposal (if any) is closed, the branch deleted and the withdrawal audited, best effort.

A candidate naming no Attribute (`source_unseen`, a Record its Source no longer sees) takes
`keep_current` only (422 otherwise): deleting the Record is an Operation of its own.

A `claim` or `policy` choice is refused (409 `reconciliation-stale`) when the Record's value
changed since detection to one none of the claims holds: detection must run again. A decision
that writes a value of an Attribute the caller may not read (a sensitive Attribute without the
clearance, a vault Attribute without `pii`) is refused (403), whether the value comes from a
claim, from the person (`custom`) or from a duplicate merge's `values`; a merge value equal to
the masked or erased placeholder is refused (422).

Reads (`view`) mask claim values, evidence quotes and decided values by clearance: wrapped
values are shown to callers cleared for their tags on the candidate's Kind, vault references
are opened as on every read, and every value of an Attribute the caller may not read is
masked.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.canonical import hex
from lorebase_core.merge import MergePolicy
from lorebase_core.merge.policies import ChangeStamp, decide_authority, decide_newest
from lorebase_core.model import (
    Attribute,
    Kind,
    ReconciliationOrigin,
    Schema,
    Sensitivity,
    builtin_link_operation_names,
    builtin_operation_names,
)
from lorebase_core.reconcile import CandidateStatus, CandidateType, value_hash
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.rbac import AccessContext
from lorebase_keeper.context import StoreAccess, require_branch, require_domain
from lorebase_keeper.errors import (
    FORBIDDEN,
    NOT_FOUND,
    VALIDATION_FAILED,
    ProblemError,
    ProblemType,
)
from lorebase_keeper.reading.sensitivity import Disclosure, Masking
from lorebase_keeper.services import branches, proposals
from lorebase_keeper.services import operations as operations_service
from lorebase_keeper.services.errors import ALREADY_EXISTS, REVIEW_REQUIRED
from lorebase_keeper.services.operations import ExecutionOutcome, ExecutionRequest
from lorebase_keeper.services.reconciliation_settle import (
    DISMISS_ACTION,
    REOPEN_ACTION,
    RESOLVE_ACTION,
    audit,
)
from lorebase_keeper.settings import KeeperSettings
from lorebase_store.models.enums import ProposalOrigin, ProposalStatus
from lorebase_store.reads import head_schema, links_of_record
from lorebase_store.repositories import (
    CandidateRow,
    CandidateStateError,
    MaterializedRepository,
    ProposalRepository,
    ReconciliationCandidateRepository,
    wrap_decision,
)

logger = logging.getLogger("lorebase.keeper.reconciliation")

BRANCH_PREFIX = "reconcile"
"""Branches of decisions that need review: `reconcile/{candidate}-{random}`."""
BRANCH_TOKEN_BYTES = 8
BRANCH_MOVED = ProblemType("reconciliation-branch-moved", 409, "Decision branch moved")
CANDIDATE_NOT_OPEN = ProblemType("candidate-not-open", 409, "Candidate is not open")
DECISION_PENDING = ProblemType("reconciliation-pending", 409, "Decision awaiting review")
CANDIDATE_STALE = ProblemType("reconciliation-stale", 409, "Candidate is out of date")
SENSITIVE_KEY = "$sensitive"
VAULT_KEY = "$vault"

Check = Callable[[Permission, AccessContext], Awaitable[None]]
Reveal = Callable[[AsyncSession], Awaitable[Masking]]


class ReconciliationChoice(StrEnum):
    CLAIM = "claim"
    KEEP_CURRENT = "keep_current"
    CUSTOM = "custom"
    POLICY = "policy"
    MERGE = "merge"


class ReconciliationPolicy(StrEnum):
    AUTHORITY = "authority"
    NEWEST_WINS = "newest_wins"
    PREFER_CURRENT = "prefer_current"
    PREFER_INCOMING = "prefer_incoming"


class Outcome(StrEnum):
    RESOLVED = "resolved"
    PROPOSED = "proposed"
    UNDECIDED = "undecided"
    DRY_RUN = "dry_run"
    DISMISSED = "dismissed"
    REOPENED = "reopened"


@dataclass(frozen=True)
class DecisionInput:
    """What the person decided (see module doc)."""

    choice: ReconciliationChoice
    reason: str
    claim_id: str | None = None
    value: JsonValue = None
    policy: ReconciliationPolicy | None = None
    survivor: str | None = None
    values: Mapping[str, JsonValue] | None = None
    dry_run: bool = False


@dataclass
class DecisionOutcome:
    """Where a decision left its candidate."""

    status: Outcome
    candidate: CandidateRow
    claim_id: str | None = None
    revision: str | None = None
    proposal: str | None = None
    branch: str | None = None
    edits: list[dict[str, Any]] = field(default_factory=list)
    detail: str | None = None


@dataclass(frozen=True)
class Context:
    """What decisions need besides the candidate: the caller, settings and callbacks."""

    store: StoreAccess
    domain_id: str
    principal: Principal
    keeper: KeeperSettings
    check: Check
    reveal: Reveal
    proposals_path: str


# Helpers -----------------------------------------------------------------------------------


def _invalid(pointer: str, message: str) -> ProblemError:
    return ProblemError(
        VALIDATION_FAILED,
        "One or more request members are invalid.",
        extensions={"errors": [{"pointer": pointer, "message": message, "code": "invalid"}]},
    )


def not_found(candidate_id: str) -> ProblemError:
    return ProblemError(NOT_FOUND, f"No reconciliation candidate {candidate_id!r}.")


def not_open(error: CandidateStateError) -> ProblemError:
    return ProblemError(
        CANDIDATE_NOT_OPEN,
        str(error),
        extensions={"candidate": error.candidate_id, "status": error.status},
    )


def branch_of(candidate_id: str) -> str:
    """A fresh, unguessable branch name for one decision on `candidate_id`."""
    return f"{BRANCH_PREFIX}/{candidate_id}-{secrets.token_hex(BRANCH_TOKEN_BYTES)}"


def raw_value(stored: Any) -> Any:
    """A stored claim value without its sensitivity wrapper (it may be a vault reference)."""
    if isinstance(stored, dict) and len(stored) == 1 and SENSITIVE_KEY in stored:
        body = stored[SENSITIVE_KEY]
        if isinstance(body, dict):
            return body.get("value")
    return stored


def _is_reference(value: Any) -> bool:
    return isinstance(value, dict) and VAULT_KEY in value


def is_stale(candidate: CandidateRow, current_hash: str | None) -> bool:
    """Whether the Record's value changed since detection to one none of the claims holds."""
    if current_hash is None:
        return False
    return current_hash not in {c.get("value_hash") for c in candidate.claims}


def unreadable(masking: Masking, attribute: Attribute, kind_id: str) -> bool:
    """Whether the caller may not read values of `attribute` of the Kind `kind_id`: a
    sensitive Attribute without the clearance, a vault Attribute without `pii`."""
    vault = attribute.in_vault and not masking.clearance.covers((Sensitivity.PII,), [kind_id])
    return vault or masking.hidden(attribute, [kind_id])


def stored_attribute(kind: Kind, name: str) -> Attribute:
    """The stored Attribute of `kind` a duplicate merge's `values` entry `name` sets; any
    other name (unknown, derived, or an Operation parameter such as `key`) is refused (422)."""
    attribute = kind.attribute_by_api_name(name)
    if attribute is None or attribute.is_derived:
        raise _invalid(f"/body/values/{name}", f"is not a stored Attribute of Kind {kind.api_name}")
    return attribute


def placeholder(masking: Masking, value: Any) -> bool:
    """Whether `value` is what reads show instead of a value (masked or erased)."""
    settings = masking.settings
    return isinstance(value, str) and value in (settings.masked_value, settings.erased_value)


def _uncleared(candidate_id: str) -> ProblemError:
    return ProblemError(
        FORBIDDEN,
        "This decision writes a value of an Attribute the caller may not read; a cleared "
        "reviewer must decide it.",
        extensions={"candidate": candidate_id},
    )


def _now(store: StoreAccess) -> datetime:
    at = store.clock()
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _claim(candidate: CandidateRow, claim_id: str, pointer: str) -> dict[str, Any]:
    for claim in candidate.claims:
        if claim.get("claim_id") == claim_id:
            return claim
    raise _invalid(pointer, f"{claim_id!r} is not a claim of candidate {candidate.id}")


def _stamp(claim: Mapping[str, Any], *, by_period: bool) -> ChangeStamp:
    when = str((claim.get("valid_from") if by_period else None) or claim.get("at"))
    at = datetime.fromisoformat(when.replace("Z", "+00:00"))
    labels = {str(claim["source"])} if claim.get("source") else set()
    return ChangeStamp(
        str(claim["claim_id"]),
        at if at.tzinfo is not None else at.replace(tzinfo=UTC),
        frozenset(labels),
    )


def _fold(
    claims: Sequence[Mapping[str, Any]],
    decide: Callable[[ChangeStamp, ChangeStamp], Any],
    stamp: Callable[[Mapping[str, Any]], ChangeStamp],
) -> Mapping[str, Any] | None:
    """The claim that wins every pairwise decision, or None when the best ones tie."""
    distinct: dict[str, Mapping[str, Any]] = {}
    for claim in claims:
        distinct.setdefault(str(claim.get("value_hash")), claim)
    listed = list(distinct.values())
    if not listed:
        return None
    best = listed[0]
    tied = False
    for other in listed[1:]:
        decision = decide(stamp(best), stamp(other))
        if decision is None:
            tied = True
        elif decision.side.value == "theirs":
            best, tied = other, False
    return None if tied else best


def authority_ranking(policy: MergePolicy, attribute: Attribute | None) -> tuple[str, ...]:
    """Source identities in authority order: the Attribute's `authority` Source first, then
    the Domain merge policy's rankings for the Attribute and overall."""
    head: tuple[str, ...] = ()
    if attribute is not None and attribute.authority:
        head = (f"source:{attribute.authority}",)
    per_attribute = () if attribute is None else policy.authority_attributes.get(attribute.id, ())
    return tuple(dict.fromkeys((*head, *per_attribute, *policy.authority)))


def apply_policy(
    candidate: CandidateRow,
    policy: ReconciliationPolicy,
    *,
    ranking: tuple[str, ...],
    current_hash: str | None,
) -> Mapping[str, Any] | None:
    """The claim `policy` picks among the candidate's claims; None when it cannot decide."""
    claims = candidate.claims
    if policy is ReconciliationPolicy.AUTHORITY:
        return _fold(
            claims,
            lambda ours, theirs: decide_authority(ranking, ours, theirs),
            lambda c: _stamp(c, by_period=False),
        )
    if policy is ReconciliationPolicy.NEWEST_WINS:
        return _fold(claims, decide_newest, lambda c: _stamp(c, by_period=True))
    hashes = {str(c.get("value_hash")) for c in claims}
    if len(hashes) != 2:
        raise _invalid("/body/policy", f"{policy.value} needs a candidate with two claims")
    current = [c for c in claims if c.get("value_hash") == current_hash]
    incoming = [c for c in claims if c.get("value_hash") != current_hash]
    if not current or not incoming:
        return None
    return current[0] if policy is ReconciliationPolicy.PREFER_CURRENT else incoming[0]


def _builtin(kind: Kind, action: str) -> str:
    for name in builtin_operation_names(kind):
        if name.startswith(f"{action}_"):
            return name
    raise _invalid("/body/choice", f"Kind {kind.api_name} has no built-in {action} Operation")


@dataclass(frozen=True)
class _Target:
    """The candidate's Kind and Attribute in the schema of its branch, and the current value."""

    schema: Schema
    kind: Kind
    attribute: Attribute | None
    current: Any
    current_hash: str | None
    tags: frozenset[Sensitivity] = frozenset()
    """The Attribute's tags in the schema read and the reference schema."""

    @property
    def vault(self) -> bool:
        return self.attribute is not None and self.attribute.in_vault


async def _target(session: AsyncSession, ctx: Context, candidate: CandidateRow) -> _Target:
    head = await head_schema(session, ctx.domain_id, candidate.branch, runtime=ctx.store.runtime)
    if head is None:
        raise ProblemError(NOT_FOUND, f"Branch {candidate.branch!r} has no materialized head.")
    schema = head.schema
    kind = schema.kinds.get(candidate.kind_id)
    if kind is None:
        raise _invalid("/", f"the Kind of candidate {candidate.id} is gone from the schema")
    attribute = (
        None if candidate.attribute_id is None else kind.attributes.get(candidate.attribute_id)
    )
    current: Any = None
    if attribute is not None:
        rows = MaterializedRepository(
            session, ctx.domain_id, candidate.branch, writes=ctx.store.runtime.settings.writes
        )
        found = await rows.records(kind.id, candidate.keys[:1])
        record = found.get(candidate.keys[0])
        current = None if record is None else record.values.get(attribute.id)
    return _Target(
        schema, kind, attribute, current, None if current is None else value_hash(current)
    )


# Reads -------------------------------------------------------------------------------------


def _cleared(masking: Masking, kind_id: str) -> Callable[[frozenset[str]], bool]:
    def check(tags: frozenset[str]) -> bool:
        try:
            parsed = {Sensitivity(t) for t in tags}
        except ValueError:
            return False
        return bool(parsed) and masking.clearance.covers(parsed, [kind_id])

    return check


def _unwrap(node: Any, cleared: Callable[[frozenset[str]], bool], masked: str) -> Any:
    if isinstance(node, dict):
        if len(node) == 1 and SENSITIVE_KEY in node and isinstance(node[SENSITIVE_KEY], dict):
            body = node[SENSITIVE_KEY]
            tags = frozenset(str(t) for t in body.get("tags") or ())
            inner = body.get("value")
            if not cleared(tags):
                return None if inner is None else masked
            return _unwrap(inner, cleared, masked)
        return {k: _unwrap(v, cleared, masked) for k, v in node.items()}
    if isinstance(node, list):
        return [_unwrap(v, cleared, masked) for v in node]
    return node


_LOCATORS = frozenset({"document_id", "chunk", "start", "end", "page", "offsets"})


def _mask_claim(claim: dict[str, Any], masked: str) -> dict[str, Any]:
    out = dict(claim)
    if out.get("value") is not None:
        out["value"] = masked
    evidence = out.get("evidence")
    if isinstance(evidence, dict):
        out["evidence"] = {
            k: (v if k in _LOCATORS or v is None else masked) for k, v in evidence.items()
        }
    return out


def _hidden(masking: Masking, schema: Schema | None, candidate: CandidateRow) -> bool:
    if schema is None or candidate.attribute_id is None:
        return False
    kind = schema.kinds.get(candidate.kind_id)
    attribute = None if kind is None else kind.attributes.get(candidate.attribute_id)
    return attribute is not None and masking.hidden(attribute, [candidate.kind_id])


def _mask_signals(signals: dict[str, Any], masked: str) -> dict[str, Any]:
    return {
        name: (
            {**signal, "quote": masked}
            if isinstance(signal, dict) and signal.get("quote") is not None
            else signal
        )
        for name, signal in signals.items()
    }


def shown(candidate: CandidateRow, schema: Schema | None, masking: Masking) -> dict[str, Any]:
    """Claims, signals and decision of `candidate` as the caller may see them (module doc),
    before vault references are opened (`Disclosure`). The quote a signal carries
    (`evidence_gone`) is masked like evidence quotes."""
    masked = masking.masked
    cleared = _cleared(masking, candidate.kind_id)
    claims = [_unwrap(c, cleared, masked) for c in candidate.claims]
    signals = _unwrap(dict(candidate.signals or {}), cleared, masked)
    decision = None if candidate.decision is None else _unwrap(candidate.decision, cleared, masked)
    if _hidden(masking, schema, candidate):
        claims = [_mask_claim(c, masked) for c in claims]
        signals = _mask_signals(signals, masked)
        if isinstance(decision, dict):
            decision = _mask_decision(decision, masked)
    return {"claims": claims, "signals": signals, "decision": decision}


def _mask_decision(decision: dict[str, Any], masked: str) -> dict[str, Any]:
    out = dict(decision)
    if out.get("value") is not None:
        out["value"] = masked
    if isinstance(out.get("values"), dict):
        out["values"] = dict.fromkeys(out["values"], masked)
    if isinstance(out.get("claims"), list):
        out["claims"] = [_mask_claim(c, masked) for c in out["claims"] if isinstance(c, dict)]
    if isinstance(out.get("previous"), dict):
        out["previous"] = _mask_decision(out["previous"], masked)
    return out


async def disclose(
    session: AsyncSession,
    ctx: Context,
    schemas: Sequence[Schema],
    masking: Masking,
    data: JsonValue,
) -> None:
    """Open the vault references left in `data`, in place, as every read does."""
    await Disclosure(session, ctx.domain_id, schemas, masking, ctx.store.runtime.vault).resolve(
        data
    )


# Decisions ---------------------------------------------------------------------------------


async def _load(session: AsyncSession, ctx: Context, candidate_id: str) -> CandidateRow:
    await require_domain(session, ctx.domain_id)
    found = await ReconciliationCandidateRepository(session, ctx.domain_id).get(candidate_id)
    if found is None:
        raise not_found(candidate_id)
    return found


async def get(ctx: Context, candidate_id: str) -> CandidateRow:
    async with ctx.store.sessions() as session, session.begin():
        return await _load(session, ctx, candidate_id)


async def _record(
    session: AsyncSession,
    ctx: Context,
    candidate: CandidateRow,
    decision: Mapping[str, Any],
    reason: str,
    tags: frozenset[Sensitivity],
    **details: Any,
) -> CandidateRow:
    at = _now(ctx.store)
    await _require_decidable(session, ctx, candidate.id)
    try:
        row = await ReconciliationCandidateRepository(session, ctx.domain_id).resolve(
            candidate.id,
            decision=wrap_decision(decision, tags),
            reason=reason,
            by=ctx.principal.id,
            at=at,
        )
    except CandidateStateError as error:
        raise not_open(error) from None
    if row is None:
        raise not_found(candidate.id)
    await audit(
        session,
        ctx.domain_id,
        actor=ctx.principal.id,
        action=RESOLVE_ACTION,
        target=candidate.id,
        at=at,
        choice=decision.get("choice"),
        claim_id=decision.get("claim_id"),
        policy=decision.get("policy"),
        **details,
    )
    return row


def _require_open(candidate: CandidateRow) -> None:
    if candidate.status != CandidateStatus.OPEN.value:
        raise ProblemError(
            CANDIDATE_NOT_OPEN,
            f"candidate {candidate.id} is {candidate.status}: cannot resolve",
            extensions={"candidate": candidate.id, "status": candidate.status},
        )


async def _awaiting(session: AsyncSession, ctx: Context, candidate: CandidateRow) -> str | None:
    """The open Proposal carrying the candidate's pending decision, if there is one."""
    if candidate.proposal_id is None or not (candidate.decision or {}).get("pending"):
        return None
    found = await ProposalRepository(session, ctx.domain_id).get(candidate.proposal_id)
    return found.id if found is not None and found.status is ProposalStatus.OPEN else None


def _pending(candidate_id: str, proposal_id: str) -> ProblemError:
    return ProblemError(
        DECISION_PENDING,
        f"candidate {candidate_id} has a decision awaiting review in Proposal {proposal_id}; "
        "merge or close that Proposal first.",
        extensions={"candidate": candidate_id, "proposal": proposal_id},
    )


async def _require_decidable(session: AsyncSession, ctx: Context, candidate_id: str) -> None:
    """Lock the candidate and refuse a decision while it is not open or its pending decision's
    Proposal is open (module doc); a pending decision of a closed Proposal is replaced."""
    row = await ReconciliationCandidateRepository(session, ctx.domain_id).get(
        candidate_id, for_update=True
    )
    if row is None:
        raise not_found(candidate_id)
    _require_open(row)
    proposal = await _awaiting(session, ctx, row)
    if proposal is not None:
        raise _pending(candidate_id, proposal)


async def resolve(ctx: Context, candidate_id: str, decision: DecisionInput) -> DecisionOutcome:
    """Decide one candidate (module doc)."""
    if not decision.reason.strip():
        raise _invalid("/body/reason", "a decision needs a reason")
    async with ctx.store.sessions() as session, session.begin():
        candidate = await _load(session, ctx, candidate_id)
        _require_open(candidate)
        awaiting = await _awaiting(session, ctx, candidate)
        if awaiting is not None:
            raise _pending(candidate.id, awaiting)
        target = await _target(session, ctx, candidate)
        masking = (await ctx.reveal(session)).over(target.schema)
        if target.attribute is not None:
            target = replace(target, tags=masking.tags(target.attribute))
        domain = await require_domain(session, ctx.domain_id)
        merge_policy = proposals.domain_policy(domain)
    duplicate = candidate.type == CandidateType.DUPLICATE.value
    if duplicate != (decision.choice is ReconciliationChoice.MERGE):
        expected = "merge" if duplicate else "claim, keep_current, custom or policy"
        raise _invalid("/body/choice", f"a {candidate.type} candidate takes {expected}")
    if decision.choice is ReconciliationChoice.MERGE:
        return await _merge_duplicates(ctx, candidate, target, decision, masking)
    if candidate.attribute_id is None and decision.choice is not ReconciliationChoice.KEEP_CURRENT:
        raise _invalid(
            "/body/choice",
            f"candidate {candidate.id} names no Attribute (a Record its Source no longer sees): "
            "it takes keep_current; dismiss it, or delete the Record through an Operation",
        )

    record: dict[str, Any] = {"choice": decision.choice.value}
    chosen: Mapping[str, Any] | None = None
    if decision.choice is ReconciliationChoice.KEEP_CURRENT:
        current = [c for c in candidate.claims if c.get("value_hash") == target.current_hash]
        if current:
            record["claim_id"] = current[0]["claim_id"]
        return await _without_write(ctx, candidate, target, decision, record)
    if decision.choice in (ReconciliationChoice.CLAIM, ReconciliationChoice.POLICY) and is_stale(
        candidate, target.current_hash
    ):
        raise ProblemError(
            CANDIDATE_STALE,
            f"The value of candidate {candidate.id} changed since it was detected to one none "
            "of its claims holds; run detection again (a reconciliation sweep) before "
            "deciding.",
            extensions={"candidate": candidate.id},
        )
    if decision.choice is ReconciliationChoice.CLAIM:
        if decision.claim_id is None:
            raise _invalid("/body/claim_id", "required for the choice claim")
        chosen = _claim(candidate, decision.claim_id, "/body/claim_id")
    elif decision.choice is ReconciliationChoice.POLICY:
        if decision.policy is None:
            raise _invalid("/body/policy", "required for the choice policy")
        record["policy"] = decision.policy.value
        chosen = apply_policy(
            candidate,
            decision.policy,
            ranking=authority_ranking(merge_policy, target.attribute),
            current_hash=target.current_hash,
        )
        if chosen is None:
            return DecisionOutcome(
                Outcome.UNDECIDED,
                candidate,
                detail=f"The policy {decision.policy.value} cannot tell the claims apart; "
                "the candidate stays open.",
            )
    elif decision.value is None:
        raise _invalid("/body/value", "required for the choice custom")
    if chosen is not None:
        record["claim_id"] = chosen["claim_id"]
        if chosen.get("value_hash") == target.current_hash:
            return await _without_write(ctx, candidate, target, decision, record)
    if target.attribute is not None and unreadable(masking, target.attribute, candidate.kind_id):
        raise _uncleared(candidate.id)
    if chosen is not None:
        value = raw_value(chosen.get("value"))
        if _is_reference(value):
            value = await _opened(ctx, target, candidate, value)
    else:
        value = decision.value
        if not target.vault:
            record["value"] = value  # a vault value is kept in the vault only
    return await _write(ctx, candidate, target, decision, record, chosen, value)


async def _opened(
    ctx: Context, target: _Target, candidate: CandidateRow, reference: Any
) -> JsonValue:
    """The value behind the vault reference of a claim; 403 when the caller may not read it."""
    holder: dict[str, Any] = {"value": reference}
    async with ctx.store.sessions() as session, session.begin():
        masking = (await ctx.reveal(session)).over(target.schema)
        disclosure = Disclosure(
            session, ctx.domain_id, (target.schema,), masking, ctx.store.runtime.vault
        )
        assert target.attribute is not None
        await disclosure.slots([(holder, "value", candidate.kind_id, target.attribute.id)])
    opened = holder["value"]
    if opened in (masking.masked, masking.settings.erased_value):
        raise ProblemError(
            FORBIDDEN,
            "Choosing this claim writes a value of a vault Attribute the caller may not read; "
            "a cleared reviewer must decide it.",
            extensions={"candidate": candidate.id},
        )
    return opened  # type: ignore[no-any-return]


async def _without_write(
    ctx: Context,
    candidate: CandidateRow,
    target: _Target,
    decision: DecisionInput,
    record: dict[str, Any],
) -> DecisionOutcome:
    if decision.dry_run:
        return DecisionOutcome(Outcome.DRY_RUN, candidate, claim_id=record.get("claim_id"))
    async with ctx.store.sessions() as session, session.begin():
        await require_domain(session, ctx.domain_id)
        row = await _record(
            session, ctx, candidate, record, decision.reason, target.tags, write=False
        )
    return DecisionOutcome(Outcome.RESOLVED, row, claim_id=record.get("claim_id"))


def _parameters(
    target: _Target, key: str, value: JsonValue, chosen: Mapping[str, Any] | None
) -> dict[str, JsonValue]:
    assert target.attribute is not None
    name = target.attribute.api_name
    parameters: dict[str, JsonValue] = {"key": key, name: value}
    if chosen is not None and target.attribute.has_valid_time:
        start, stop = chosen.get("valid_from"), chosen.get("valid_to")
        if start is not None or stop is not None:
            parameters["valid"] = {name: {"from": start, "to": stop}}
    return parameters


def _origin(
    ctx: Context, candidate: CandidateRow, decision: DecisionInput, chosen: Mapping[str, Any] | None
) -> ReconciliationOrigin:
    resolver = (
        f"policy:{decision.policy.value}"
        if decision.choice is ReconciliationChoice.POLICY and decision.policy is not None
        else f"principal:{ctx.principal.id}"
    )
    return ReconciliationOrigin(
        candidate_id=candidate.id,
        claim_id=None if chosen is None else str(chosen["claim_id"]),
        resolver=resolver,
        source=None if chosen is None else chosen.get("source"),
    )


async def _write(
    ctx: Context,
    candidate: CandidateRow,
    target: _Target,
    decision: DecisionInput,
    record: dict[str, Any],
    chosen: Mapping[str, Any] | None,
    value: JsonValue,
) -> DecisionOutcome:
    """Write the chosen value with `update_{kind}`; propose it when the branch needs review."""
    operation = _builtin(target.kind, "update")
    request = ExecutionRequest(
        operation=operation,
        parameters=_parameters(target, candidate.keys[0], value, chosen),
        branch=candidate.branch,
        dry_run=decision.dry_run,
        message=f"Reconcile {candidate.id}",
        reconciliation=_origin(ctx, candidate, decision, chosen),
    )
    tags = target.tags
    saved: list[CandidateRow] = []

    async def within(session: AsyncSession, outcome: ExecutionOutcome) -> None:
        revision = hex(outcome.revisions[-1]) if outcome.revisions else None
        saved.append(
            await _record(
                session,
                ctx,
                candidate,
                {**record, "revision": revision, "invocation_id": outcome.invocation_id},
                decision.reason,
                tags,
                write=True,
                revision=revision,
            )
        )

    try:
        executed = await operations_service.execute(
            ctx.store,
            ctx.domain_id,
            request,
            ctx.principal,
            ctx.keeper.operations,
            check=ctx.check,
            proposals_path=ctx.proposals_path,
            reveal=ctx.reveal,
            within=within,
        )
    except ProblemError as error:
        if error.problem is not REVIEW_REQUIRED:
            raise
        return await _propose(ctx, candidate, decision, record, tags, [request])
    if decision.dry_run:
        if executed.violations:
            raise operations_service.refused(executed, limit=ctx.keeper.operations.max_issues)
        return DecisionOutcome(
            Outcome.DRY_RUN,
            candidate,
            claim_id=record.get("claim_id"),
            branch=candidate.branch,
            edits=executed.edits,
        )
    revision = hex(executed.revisions[-1]) if executed.revisions else None
    return DecisionOutcome(
        Outcome.RESOLVED,
        saved[0] if saved else candidate,
        claim_id=record.get("claim_id"),
        revision=revision,
        branch=candidate.branch,
        edits=executed.edits,
    )


async def _open_branch(ctx: Context, candidate: CandidateRow) -> tuple[str, bytes]:
    """A new branch `reconcile/{candidate}-{random}` from the candidate's branch, and its head.
    The name is fresh for every decision: a branch someone prepared is never reused."""
    name = branch_of(candidate.id)
    await ctx.check(Permission.PROPOSAL_CREATE, AccessContext(branch=name))
    await ctx.check(Permission.BRANCH_CREATE, AccessContext(branch=name))
    created = await branches.create(
        ctx.store,
        ctx.domain_id,
        name,
        ctx.principal,
        ctx.keeper.branches,
        source=candidate.branch,
        revision=None,
        idempotency_key=None,
    )
    if created.replayed:
        raise ProblemError(ALREADY_EXISTS, f"Branch {name!r} exists already.")
    return name, created.ref.head


async def _proposal_into(ctx: Context, candidate: CandidateRow, name: str, title: str) -> str:
    """A new Proposal of `name` into the candidate's branch."""
    created = await proposals.create(
        ctx.store,
        ctx.domain_id,
        proposals.NewProposal(
            source_ref=name,
            target_ref=candidate.branch,
            title=title,
            description=f"Decision on reconciliation candidate {candidate.id}.",
            origin=ProposalOrigin.OPERATION,
        ),
        ctx.principal,
        ctx.keeper.proposals,
        idempotency_key=None,
        reconciliation_check=ctx.keeper.reconciliation.merge_check,
    )
    if created.replayed:
        raise ProblemError(ALREADY_EXISTS, f"A Proposal of {name!r} exists already.")
    return created.view.proposal.id


async def _propose(
    ctx: Context,
    candidate: CandidateRow,
    decision: DecisionInput,
    record: dict[str, Any],
    tags: frozenset[Sensitivity],
    requests: Sequence[ExecutionRequest],
    *,
    title: str | None = None,
) -> DecisionOutcome:
    """Run `requests` on a new branch `reconcile/{candidate}-{random}` and propose it into the
    candidate's branch; the candidate stays open, linked to the Proposal, with the decision
    pending (branch, base and head recorded)."""
    if decision.dry_run:
        edits: list[dict[str, Any]] = []
        for request in requests:
            dry = await operations_service.execute(
                ctx.store,
                ctx.domain_id,
                _on(request, candidate.branch, dry_run=True),
                ctx.principal,
                ctx.keeper.operations,
                check=ctx.check,
                proposals_path=ctx.proposals_path,
                reveal=ctx.reveal,
            )
            if dry.violations:
                raise operations_service.refused(dry, limit=ctx.keeper.operations.max_issues)
            edits.extend(dry.edits)
        return DecisionOutcome(
            Outcome.DRY_RUN,
            candidate,
            claim_id=record.get("claim_id"),
            edits=edits,
        )
    name, base = await _open_branch(ctx, candidate)
    proposal_id: str | None = None
    try:
        head = base
        revisions: list[str] = []
        edits = []
        for request in requests:
            # Each write expects the head the previous one left: nothing else lands on the branch.
            executed = await operations_service.execute(
                ctx.store,
                ctx.domain_id,
                _on(request, name, dry_run=False, expected_head=head),
                ctx.principal,
                ctx.keeper.operations,
                check=ctx.check,
                proposals_path=ctx.proposals_path,
                reveal=ctx.reveal,
            )
            head = executed.head if not executed.revisions else executed.revisions[-1]
            revisions.extend(hex(r) for r in executed.revisions)
            edits.extend(executed.edits)
        async with ctx.store.sessions() as session, session.begin():
            await require_domain(session, ctx.domain_id)
            current = await require_branch(session, ctx.domain_id, name)
        if current.head != head:
            raise ProblemError(
                BRANCH_MOVED,
                f"Branch {name!r} moved while the decision was written; decide again.",
                extensions={"branch": name},
            )
        proposal_id = await _proposal_into(
            ctx, candidate, name, title or f"Reconcile {candidate.id}"
        )
        record = {**record, "branch": name, "base": hex(base), "head": hex(head)}
        at = _now(ctx.store)
        async with ctx.store.sessions() as session, session.begin():
            await require_domain(session, ctx.domain_id)
            # Another decision may have been proposed meanwhile: its Proposal stays, this one
            # is withdrawn.
            await _require_decidable(session, ctx, candidate.id)
            try:
                row = await ReconciliationCandidateRepository(session, ctx.domain_id).set_pending(
                    candidate.id,
                    proposal_id,
                    decision=wrap_decision(record, tags),
                    reason=decision.reason,
                    by=ctx.principal.id,
                    at=at,
                )
            except CandidateStateError as error:
                raise not_open(error) from None
            if row is None:
                raise not_found(candidate.id)
            await audit(
                session,
                ctx.domain_id,
                actor=ctx.principal.id,
                action=RESOLVE_ACTION,
                target=candidate.id,
                at=at,
                choice=record.get("choice"),
                claim_id=record.get("claim_id"),
                policy=record.get("policy"),
                status=Outcome.PROPOSED.value,
                branch=name,
                proposal=proposal_id,
                revisions=revisions,
            )
    except Exception as error:
        moved = isinstance(error, ProblemError) and error.problem is BRANCH_MOVED
        await _withdraw(ctx, candidate, record, name, proposal_id, keep_branch=moved)
        raise
    return DecisionOutcome(
        Outcome.PROPOSED,
        row,
        claim_id=record.get("claim_id"),
        revision=revisions[-1] if revisions else None,
        proposal=proposal_id,
        branch=name,
        edits=edits,
    )


async def _withdraw(
    ctx: Context,
    candidate: CandidateRow,
    record: Mapping[str, Any],
    name: str,
    proposal_id: str | None,
    *,
    keep_branch: bool,
) -> None:
    """Best effort after a proposed decision failed: close its Proposal, delete its branch
    (unless someone else wrote to it) and audit the withdrawal of an opened Proposal."""
    if proposal_id is not None:
        try:
            await proposals.close(ctx.store, ctx.domain_id, proposal_id, ctx.keeper.proposals)
        except Exception:
            logger.warning(
                "could not close Proposal %s of a failed decision on %s",
                proposal_id,
                candidate.id,
                exc_info=True,
            )
    if not keep_branch:
        try:
            await branches.delete(ctx.store, ctx.domain_id, name, ctx.keeper.branches)
        except Exception:
            logger.warning(
                "could not delete branch %s of a failed decision on %s",
                name,
                candidate.id,
                exc_info=True,
            )
    if proposal_id is None:
        return
    try:
        at = _now(ctx.store)
        async with ctx.store.sessions() as session, session.begin():
            await require_domain(session, ctx.domain_id)
            await audit(
                session,
                ctx.domain_id,
                actor=ctx.principal.id,
                action=RESOLVE_ACTION,
                target=candidate.id,
                at=at,
                outcome="rejected",
                status="withdrawn",
                choice=record.get("choice"),
                branch=name,
                proposal=proposal_id,
            )
    except Exception:
        logger.warning("could not audit the withdrawn decision on %s", candidate.id, exc_info=True)


def _on(
    request: ExecutionRequest, branch: str, *, dry_run: bool, expected_head: bytes | None = None
) -> ExecutionRequest:
    return ExecutionRequest(
        operation=request.operation,
        parameters=request.parameters,
        branch=branch,
        dry_run=dry_run,
        expected_head=expected_head,
        message=request.message,
        reviewed_by_proposal=True,
        reconciliation=request.reconciliation,
    )


# Duplicates --------------------------------------------------------------------------------


async def _merge_duplicates(
    ctx: Context,
    candidate: CandidateRow,
    target: _Target,
    decision: DecisionInput,
    masking: Masking,
) -> DecisionOutcome:
    """Survivor update, Links re-created on it and the absorbed Record deleted, proposed.
    `values` may only name stored Attributes of the Kind (422), none the caller may not read
    (403), and may not hold a masked or erased placeholder (422)."""
    keys = list(candidate.keys)
    if decision.survivor not in keys or len(keys) != 2:
        raise _invalid("/body/survivor", f"must be one of {', '.join(keys)}")
    survivor = decision.survivor
    absorbed = next(k for k in keys if k != survivor)
    kind = target.kind
    origin = ReconciliationOrigin(
        candidate_id=candidate.id, resolver=f"principal:{ctx.principal.id}"
    )
    message = f"Reconcile {candidate.id}: merge {kind.api_name}/{absorbed} into {survivor}"
    requests: list[ExecutionRequest] = []
    values = dict(decision.values or {})
    for name, value in values.items():
        attribute = stored_attribute(kind, name)
        if unreadable(masking, attribute, kind.id):
            raise _uncleared(candidate.id)
        if placeholder(masking, value):
            raise _invalid(
                f"/body/values/{name}",
                "is the placeholder reads show for a value the caller may not see, not a value",
            )
    if any(a.is_sensitive for a in kind.attributes.values() if a.api_name in values):
        values_kept: dict[str, JsonValue] = {}
    else:
        values_kept = values
    if values:
        requests.append(
            ExecutionRequest(
                operation=_builtin(kind, "update"),
                parameters={**values, "key": survivor},
                message=message,
                reconciliation=origin,
            )
        )
    requests.extend(
        await _relinked(ctx, candidate, target.schema, kind, absorbed, survivor, message, origin)
    )
    requests.append(
        ExecutionRequest(
            operation=_builtin(kind, "delete"),
            parameters={"key": absorbed},
            message=message,
            reconciliation=origin,
        )
    )
    record: dict[str, Any] = {
        "choice": ReconciliationChoice.MERGE.value,
        "survivor": survivor,
        "absorbed": absorbed,
        "attributes": sorted(values),
        "values": values_kept,
    }
    return await _propose(
        ctx,
        candidate,
        decision,
        record,
        frozenset(),
        requests,
        title=f"Merge {kind.api_name} {absorbed} into {survivor}",
    )


async def _relinked(
    ctx: Context,
    candidate: CandidateRow,
    schema: Schema,
    kind: Kind,
    absorbed: str,
    survivor: str,
    message: str,
    origin: ReconciliationOrigin,
) -> list[ExecutionRequest]:
    """`import_links` of the stored Links of `absorbed`, with `survivor` in its place."""
    cap = ctx.store.runtime.settings.reads.max_page_rows
    async with ctx.store.sessions() as session, session.begin():
        await require_domain(session, ctx.domain_id)
        found = await links_of_record(
            session,
            ctx.domain_id,
            candidate.branch,
            schema,
            kind.id,
            absorbed,
            limit=cap,
            runtime=ctx.store.runtime,
        )
    if len(found) >= cap:
        raise _invalid(
            "/body/survivor", f"{kind.api_name}/{absorbed} has more than {cap} Links to move"
        )
    rows: dict[str, list[dict[str, JsonValue]]] = {}
    for link in found:
        relation = schema.relations.get(link.relation_id)
        if relation is None or not builtin_link_operation_names(relation):
            continue
        if link.object_hash is None:
            continue  # foreign-key backed: moves with the values of the Records
        start = survivor if link.from_key == absorbed else link.from_key
        end = survivor if link.to_key == absorbed else link.to_key
        if start == end:
            continue
        values = link.values or {}
        row: dict[str, JsonValue] = {"from": start, "to": end}
        for attribute in relation.attributes.values():
            if attribute.id in values:
                row[attribute.api_name] = values[attribute.id]
        rows.setdefault(relation.api_name, []).append(row)
    return [
        ExecutionRequest(
            operation="import_links",
            parameters={"relation": name, "rows": listed},  # type: ignore[dict-item]
            message=message,
            reconciliation=origin,
        )
        for name, listed in sorted(rows.items())
    ]


# Dismissal and reopening -------------------------------------------------------------------


async def dismiss(ctx: Context, candidate_id: str, reason: str) -> DecisionOutcome:
    return await _status(ctx, candidate_id, reason, reopen=False)


async def reopen(ctx: Context, candidate_id: str, reason: str) -> DecisionOutcome:
    return await _status(ctx, candidate_id, reason, reopen=True)


async def _status(ctx: Context, candidate_id: str, reason: str, *, reopen: bool) -> DecisionOutcome:
    if not reason.strip():
        raise _invalid("/body/reason", "a decision needs a reason")
    at = _now(ctx.store)
    async with ctx.store.sessions() as session, session.begin():
        await _load(session, ctx, candidate_id)
        repo = ReconciliationCandidateRepository(session, ctx.domain_id)
        decide = repo.reopen if reopen else repo.dismiss
        try:
            row = await decide(candidate_id, reason=reason, by=ctx.principal.id, at=at)
        except CandidateStateError as error:
            raise not_open(error) from None
        if row is None:
            raise not_found(candidate_id)
        await audit(
            session,
            ctx.domain_id,
            actor=ctx.principal.id,
            action=REOPEN_ACTION if reopen else DISMISS_ACTION,
            target=candidate_id,
            at=at,
        )
    return DecisionOutcome(Outcome.REOPENED if reopen else Outcome.DISMISSED, row)


# Bulk --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BulkItem:
    """What one decision of a bulk request did: its outcome, or the problem that stopped it."""

    candidate: str
    status: str
    detail: str | None = None
    proposal: str | None = None
    revision: str | None = None


async def bulk(
    ctx: Context,
    candidate_ids: Sequence[str],
    *,
    policy: ReconciliationPolicy | None,
    reason: str,
    dry_run: bool,
) -> list[BulkItem]:
    """Apply one choice to each candidate on its own: `policy`, or a dismissal when it is
    None. A candidate that cannot be decided is reported and the others go on."""
    items: list[BulkItem] = []
    for candidate_id in candidate_ids:
        try:
            if policy is None:
                if dry_run:
                    found = await get(ctx, candidate_id)
                    status = (
                        Outcome.DRY_RUN.value
                        if found.status == CandidateStatus.OPEN.value
                        else "failed"
                    )
                    items.append(BulkItem(candidate_id, status))
                    continue
                outcome = await dismiss(ctx, candidate_id, reason)
            else:
                outcome = await resolve(
                    ctx,
                    candidate_id,
                    DecisionInput(
                        ReconciliationChoice.POLICY, reason, policy=policy, dry_run=dry_run
                    ),
                )
        except ProblemError as error:
            items.append(BulkItem(candidate_id, "failed", error.problem_detail))
            continue
        items.append(
            BulkItem(
                candidate_id,
                outcome.status.value,
                outcome.detail,
                outcome.proposal,
                outcome.revision,
            )
        )
    return items
