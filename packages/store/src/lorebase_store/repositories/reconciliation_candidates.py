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
or `$vault` keys is wrapped with every tag, so it cannot pose as a wrapper. Readers mask them by
clearance.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from sqlalchemy import Table, and_, or_, select, update
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


@dataclass(frozen=True, slots=True)
class UpsertOutcome:
    """The stored candidate, whether this call created it, and the claims it added."""

    row: CandidateRow
    created: bool
    claims_added: int


def wrap_claim(claim: Mapping[str, Any], tags: Collection[Sensitivity]) -> dict[str, Any]:
    """`claim` with its value wrapped for storage (module doc); other members unchanged."""
    # Imported here: `outbox_events` reaches the commit pipeline, which imports repositories.
    from lorebase_store.outbox_events import carries_markers, sensitive_value

    out = dict(claim)
    if "value" not in out:
        return out
    if tags:
        out["value"] = sensitive_value(tags, out["value"])
    elif carries_markers(out["value"]):
        out["value"] = sensitive_value(EVERY_TAG, out["value"])
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
        max_claims: int,
        now: datetime,
        new_id: Callable[[], str],
    ) -> UpsertOutcome:
        """Store `draft`, or fold it into the candidate of its fingerprint (module doc)."""
        table = CANDIDATES
        fingerprint = draft.fingerprint
        incoming = [wrap_claim(c.document(), sensitivity) for c in draft.claims]
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
