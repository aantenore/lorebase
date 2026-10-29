"""Reconciliation candidates of a Domain (design 10.6).

`upsert` stores a candidate raised by a stage, keyed by `(branch, fingerprint)`: a candidate
seen again gains the claims it did not hold (the newest `max_claims` kept, every distinct
claim counted in `claim_count`), its signals are merged and, if it had become `obsolete`, it
is `open` again since the disagreement is back. A decided candidate keeps its status: a
dismissed contradiction stays dismissed until a new value (a new fingerprint) appears.

Decisions (`resolve`, `dismiss`, `reopen`, `mark_obsolete`) move a candidate along
`TRANSITIONS` and record who, when, why and the claims as they were then; a decision from a
status that does not allow it raises `CandidateStateError`. Only the pipeline marks a
candidate obsolete, always with a reason.

**Sensitive values.** Claim values of an Attribute with sensitivity tags are stored wrapped as
outbox payloads wrap them (`{"$sensitive": {"tags": [...], "value": ...}}`, a vault Attribute's
value being its vault reference); a value of an untagged Attribute whose JSON holds `$sensitive`
or `$vault` keys is wrapped with every tag, so it cannot pose as a wrapper. The evidence of such
a claim quotes the value: its members other than the locators (`EVIDENCE_LOCATORS`) are wrapped
the same way, and left out for a vault Attribute (the document and offsets stay). Values a
decision records (`value`, `values`) are wrapped like claim values (`wrap_decision`), and its
snapshot of the claims is the stored, wrapped one. Readers mask all of them by clearance.

`proposal_checks` counts the open candidates of a Proposal for the `reconciliation` merge check
(design 10.3): those it raised (`proposal_id`) and those found on its source branch, except
those whose decision it carries.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from sqlalchemy import Table, and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Sensitivity
from lorebase_core.reconcile import (
    TRANSITIONS,
    CandidateDraft,
    CandidateStatus,
    CandidateType,
    DecisionAction,
    merge_claims,
)
from lorebase_store.models import metadata

CANDIDATES: Table = metadata.tables["reconciliation_candidates"]

EVERY_TAG: tuple[Sensitivity, ...] = tuple(Sensitivity)


class CandidateStateError(Exception):
    """A decision the candidate's status does not allow."""

    def __init__(self, candidate_id: str, status: str, action: DecisionAction) -> None:
        super().__init__(f"candidate {candidate_id} is {status}: cannot {action.value}")
        self.candidate_id = candidate_id
        self.status = status
        self.action = action


@dataclass(frozen=True, slots=True)
class CandidateRow:
    id: str
    branch: str
    type: str
    status: str
    fingerprint: bytes
    kind_id: str
    keys: list[str]
    attribute_id: str | None
    claims: list[dict[str, Any]]
    signals: dict[str, Any]
    claim_count: int
    proposal_id: str | None
    decision: dict[str, Any] | None
    decided_by: str | None
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime


_FIELDS = tuple(f.name for f in fields(CandidateRow))


def _row(found: Any) -> CandidateRow:
    return CandidateRow(**{name: found[name] for name in _FIELDS})


@dataclass(frozen=True, slots=True)
class CandidateFilter:
    """Optional filters of a candidate listing; `source` matches a claim's source identity."""

    branch: str | None = None
    type: CandidateType | None = None
    status: CandidateStatus | None = None
    kind_id: str | None = None
    attribute_id: str | None = None
    source: str | None = None
    kind_ids: frozenset[str] | None = None
    """The Kinds a caller may read (grants narrowed to Kinds); `None` for every Kind."""


@dataclass(frozen=True, slots=True)
class ProposalChecks:
    """Open candidates of a Proposal by type, and the first ids (oldest first)."""

    counts: dict[str, int]
    ids: list[str]

    @property
    def open(self) -> int:
        return sum(self.counts.values())


@dataclass(frozen=True, slots=True)
class UpsertOutcome:
    """The stored candidate, whether this call created it, and the claims it added."""

    row: CandidateRow
    created: bool
    claims_added: int


#: Evidence members that locate a passage without quoting it: kept as they are.
EVIDENCE_LOCATORS = frozenset({"document_id", "chunk", "start", "end", "page", "offsets"})


def _wrap_value(value: Any, tags: Collection[Sensitivity]) -> Any:
    # Imported here: `outbox_events` reaches the commit pipeline, which imports repositories.
    from lorebase_store.outbox_events import carries_markers, sensitive_value

    if tags:
        return sensitive_value(tags, value)
    if carries_markers(value):
        return sensitive_value(EVERY_TAG, value)
    return value


def _wrap_evidence(
    evidence: Mapping[str, Any], tags: Collection[Sensitivity], *, vault: bool
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, item in evidence.items():
        if name in EVIDENCE_LOCATORS:
            out[name] = item
        elif not vault:
            out[name] = _wrap_value(item, tags) if tags else item
    return out


def wrap_claim(
    claim: Mapping[str, Any], tags: Collection[Sensitivity], *, vault: bool = False
) -> dict[str, Any]:
    """`claim` with its value and the quoting members of its evidence wrapped for storage
    (module doc); `vault` leaves the evidence quote out. Other members unchanged."""
    out = dict(claim)
    if "value" in out:
        out["value"] = _wrap_value(out["value"], tags)
    evidence = out.get("evidence")
    if isinstance(evidence, Mapping) and (tags or vault):
        out["evidence"] = _wrap_evidence(evidence, tags, vault=vault)
    return out


def wrap_decision(decision: Mapping[str, Any], tags: Collection[Sensitivity]) -> dict[str, Any]:
    """`decision` with the values it names (`value`, and each of `values`) wrapped like claim
    values; other members unchanged."""
    out = dict(decision)
    if out.get("value") is not None:
        out["value"] = _wrap_value(out["value"], tags)
    values = out.get("values")
    if isinstance(values, Mapping):
        out["values"] = {name: _wrap_value(item, tags) for name, item in values.items()}
    return out


class ReconciliationCandidateRepository:
    """Candidates of one Domain, in the caller's transaction (after `scope_to_domain`)."""

    def __init__(self, session: AsyncSession, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id

    def _scoped(self) -> Any:
        return CANDIDATES.c.domain_id == self._domain

    async def get(self, candidate_id: str, *, for_update: bool = False) -> CandidateRow | None:
        query = select(CANDIDATES).where(self._scoped(), CANDIDATES.c.id == candidate_id)
        if for_update:
            query = query.with_for_update()
        found = (await self._session.execute(query)).mappings().one_or_none()
        return None if found is None else _row(found)

    async def by_fingerprint(
        self, branch: str, fingerprint: bytes, *, for_update: bool = False
    ) -> CandidateRow | None:
        query = select(CANDIDATES).where(
            self._scoped(),
            CANDIDATES.c.branch == branch,
            CANDIDATES.c.fingerprint == fingerprint,
        )
        if for_update:
            query = query.with_for_update()
        found = (await self._session.execute(query)).mappings().one_or_none()
        return None if found is None else _row(found)

    async def upsert(
        self,
        draft: CandidateDraft,
        *,
        sensitivity: Collection[Sensitivity] = (),
        vault: bool = False,
        max_claims: int,
        now: datetime,
        new_id: Callable[[], str],
    ) -> UpsertOutcome:
        """Store `draft`, or fold it into the candidate of its fingerprint (module doc).
        `sensitivity` and `vault` describe the candidate's Attribute (`wrap_claim`)."""
        table = CANDIDATES
        fingerprint = draft.fingerprint
        incoming = [wrap_claim(c.document(), sensitivity, vault=vault) for c in draft.claims]
        merged = merge_claims([], incoming, stored_count=0, max_claims=max_claims)
        inserted = (
            (
                await self._session.execute(
                    pg_insert(table)
                    .values(
                        domain_id=self._domain,
                        id=new_id(),
                        branch=draft.branch,
                        type=draft.type.value,
                        status=CandidateStatus.OPEN.value,
                        fingerprint=fingerprint,
                        kind_id=draft.kind_id,
                        keys=list(draft.keys),
                        attribute_id=draft.attribute_id,
                        claims=merged.kept,
                        signals=dict(draft.signals),
                        claim_count=merged.count,
                        proposal_id=draft.proposal_id,
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=["domain_id", "branch", "fingerprint"])
                    .returning(*table.c)
                )
            )
            .mappings()
            .one_or_none()
        )
        if inserted is not None:
            return UpsertOutcome(_row(inserted), created=True, claims_added=merged.added)
        current = await self.by_fingerprint(draft.branch, fingerprint, for_update=True)
        if current is None:  # pragma: no cover - the conflicting row cannot vanish under RLS
            raise RuntimeError("candidate vanished during upsert")
        merged = merge_claims(
            current.claims, incoming, stored_count=current.claim_count, max_claims=max_claims
        )
        values: dict[str, Any] = {
            "claims": merged.kept,
            "claim_count": merged.count,
            "signals": {**current.signals, **draft.signals},
            "updated_at": now,
        }
        if draft.proposal_id is not None and current.proposal_id is None:
            values["proposal_id"] = draft.proposal_id
        if current.status == CandidateStatus.OBSOLETE.value:
            values["status"] = CandidateStatus.OPEN.value
        row = await self._update(current.id, values)
        return UpsertOutcome(row, created=False, claims_added=merged.added)

    async def page(
        self,
        filters: CandidateFilter,
        *,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> list[CandidateRow]:
        """Candidates matching `filters`, newest first, after the keyset `(created_at, id)`."""
        table = CANDIDATES
        query = select(table).where(self._scoped())
        for column, value in (
            (table.c.branch, filters.branch),
            (table.c.type, None if filters.type is None else filters.type.value),
            (table.c.status, None if filters.status is None else filters.status.value),
            (table.c.kind_id, filters.kind_id),
            (table.c.attribute_id, filters.attribute_id),
        ):
            if value is not None:
                query = query.where(column == value)
        if filters.source is not None:
            query = query.where(table.c.claims.contains([{"source": filters.source}]))
        if filters.kind_ids is not None:
            query = query.where(table.c.kind_id.in_(sorted(filters.kind_ids)))
        if after is not None:
            at, last_id = after
            query = query.where(
                or_(table.c.created_at < at, and_(table.c.created_at == at, table.c.id < last_id))
            )
        query = query.order_by(table.c.created_at.desc(), table.c.id.desc()).limit(limit)
        result = await self._session.execute(query)
        return [_row(found) for found in result.mappings()]

    async def for_record(
        self, branch: str, kind_id: str, key: str, *, statuses: Iterable[CandidateStatus] = ()
    ) -> list[CandidateRow]:
        """Candidates naming the Record `key` of `kind_id` on `branch` (the Record page)."""
        table = CANDIDATES
        query = select(table).where(
            self._scoped(),
            table.c.branch == branch,
            table.c.kind_id == kind_id,
            table.c["keys"].contains([key]),  # `table.c.keys` is the collection's method
        )
        wanted = [s.value for s in statuses]
        if wanted:
            query = query.where(table.c.status.in_(wanted))
        result = await self._session.execute(query.order_by(table.c.created_at, table.c.id))
        return [_row(found) for found in result.mappings()]

    async def proposal_checks(
        self,
        proposal_id: str,
        source_ref: str,
        *,
        types: Iterable[CandidateType] = (),
        limit: int = 20,
    ) -> ProposalChecks:
        """Open candidates raised by the Proposal or found on its source branch (module doc),
        of `types` (every type when empty). Candidates whose pending decision the Proposal
        carries (`set_pending`) are left out: its merge resolves them."""
        table = CANDIDATES
        resolving = and_(
            table.c.proposal_id == proposal_id,
            table.c.decision["pending"].as_boolean().is_(True),
        )
        query = select(table.c.id, table.c.type).where(
            self._scoped(),
            table.c.status == CandidateStatus.OPEN.value,
            or_(table.c.proposal_id == proposal_id, table.c.branch == source_ref),
            ~func.coalesce(resolving, False),
        )
        wanted = [t.value for t in types]
        if wanted:
            query = query.where(table.c.type.in_(wanted))
        rows = (await self._session.execute(query.order_by(table.c.created_at, table.c.id))).all()
        counts: dict[str, int] = {}
        for _id, kind in rows:
            counts[kind] = counts.get(kind, 0) + 1
        return ProposalChecks(counts, [row[0] for row in rows[:limit]])

    async def pending_for(self, proposal_id: str) -> list[CandidateRow]:
        """Open candidates waiting for the merge of the Proposal a decision opened
        (`set_pending`)."""
        table = CANDIDATES
        query = (
            select(table)
            .where(
                self._scoped(),
                table.c.status == CandidateStatus.OPEN.value,
                table.c.proposal_id == proposal_id,
                table.c.decision["pending"].as_boolean().is_(True),
            )
            .order_by(table.c.created_at, table.c.id)
            .with_for_update()
        )
        result = await self._session.execute(query)
        return [_row(found) for found in result.mappings()]

    async def set_pending(
        self,
        candidate_id: str,
        proposal_id: str,
        *,
        decision: Mapping[str, Any],
        reason: str,
        by: str,
        at: datetime,
    ) -> CandidateRow | None:
        """Record a decision whose write became the Proposal `proposal_id` (protected branch):
        the candidate stays open, linked to it, until it merges (`pending_for`)."""
        if not reason.strip():
            raise ValueError("resolve needs a reason")
        current = await self.get(candidate_id, for_update=True)
        if current is None:
            return None
        if current.status != CandidateStatus.OPEN.value:
            raise CandidateStateError(candidate_id, current.status, DecisionAction.RESOLVE)
        pending = {
            **decision,
            "action": DecisionAction.RESOLVE.value,
            "reason": reason,
            "claims": current.claims,
            "pending": True,
            "proposal": proposal_id,
        }
        return await self._update(
            candidate_id,
            {
                "proposal_id": proposal_id,
                "decision": pending,
                "decided_by": by,
                "decided_at": at,
                "updated_at": at,
            },
        )

    async def link_proposal(
        self, candidate_id: str, proposal_id: str, *, now: datetime
    ) -> CandidateRow | None:
        """Link the Proposal a decision opened (protected branch); the candidate stays open."""
        if await self.get(candidate_id) is None:
            return None
        return await self._update(candidate_id, {"proposal_id": proposal_id, "updated_at": now})

    async def resolve(
        self,
        candidate_id: str,
        *,
        decision: Mapping[str, Any],
        reason: str,
        by: str,
        at: datetime,
    ) -> CandidateRow | None:
        """Record a decision (`decision`: the choice, chosen claim, policy or value)."""
        return await self._decide(
            candidate_id, DecisionAction.RESOLVE, reason=reason, by=by, at=at, details=decision
        )

    async def dismiss(
        self, candidate_id: str, *, reason: str, by: str, at: datetime
    ) -> CandidateRow | None:
        return await self._decide(candidate_id, DecisionAction.DISMISS, reason=reason, by=by, at=at)

    async def reopen(
        self, candidate_id: str, *, reason: str, by: str, at: datetime
    ) -> CandidateRow | None:
        return await self._decide(candidate_id, DecisionAction.REOPEN, reason=reason, by=by, at=at)

    async def mark_obsolete(
        self, candidate_id: str, *, reason: str, by: str, at: datetime
    ) -> CandidateRow | None:
        """The pipeline's verdict that the disagreement left the data (design 10.6)."""
        return await self._decide(
            candidate_id, DecisionAction.OBSOLETE, reason=reason, by=by, at=at
        )

    async def _decide(
        self,
        candidate_id: str,
        action: DecisionAction,
        *,
        reason: str,
        by: str,
        at: datetime,
        details: Mapping[str, Any] | None = None,
    ) -> CandidateRow | None:
        """Move the candidate along `TRANSITIONS`; `None` when it does not exist."""
        if not reason.strip():
            raise ValueError(f"{action.value} needs a reason")
        current = await self.get(candidate_id, for_update=True)
        if current is None:
            return None
        allowed, target = TRANSITIONS[action]
        if current.status not in {s.value for s in allowed}:
            raise CandidateStateError(candidate_id, current.status, action)
        decision: dict[str, Any] = {
            **(details or {}),
            "action": action.value,
            "reason": reason,
            "claims": current.claims,
        }
        if action is DecisionAction.REOPEN and current.decision is not None:
            decision["previous"] = {k: v for k, v in current.decision.items() if k != "previous"}
        return await self._update(
            candidate_id,
            {
                "status": target.value,
                "decision": decision,
                "decided_by": by,
                "decided_at": at,
                "updated_at": at,
            },
        )

    async def _update(self, candidate_id: str, values: Mapping[str, Any]) -> CandidateRow:
        updated = (
            (
                await self._session.execute(
                    update(CANDIDATES)
                    .where(self._scoped(), CANDIDATES.c.id == candidate_id)
                    .values(**values)
                    .returning(*CANDIDATES.c)
                )
            )
            .mappings()
            .one()
        )
        return _row(updated)
