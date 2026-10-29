"""Audit entries of reconciliation decisions, and the candidates a merged Proposal resolves
(design 10.6).

A decision whose write needed review became a Proposal (`services.reconciliation`): its
candidate stays `open`, linked to the Proposal with the decision marked pending. When that
Proposal merges, `settle_merged` resolves the candidate with the decision, in the transaction
of the merge, and audits it as `reconciliation.resolve` in the name of the person who decided.
Kept apart from `services.reconciliation`, which uses the Proposal service.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.canonical import hex
from lorebase_store.auth import AuditRepository, NewAuditEntry
from lorebase_store.repositories import ReconciliationCandidateRepository

RESOLVE_ACTION = "reconciliation.resolve"
DISMISS_ACTION = "reconciliation.dismiss"
REOPEN_ACTION = "reconciliation.reopen"
SWEEP_ACTION = "reconciliation.sweep"
PENDING_MARKS = frozenset({"pending", "proposal"})
"""Members of a pending decision that the resolved decision leaves out."""


async def audit(
    session: AsyncSession,
    domain_id: str,
    *,
    actor: str,
    action: str,
    target: str,
    at: datetime,
    **details: Any,
) -> None:
    """Append one entry to the Domain's hash-chained audit log, in `session`'s transaction."""
    await AuditRepository(session).append(
        NewAuditEntry(
            domain_id=domain_id,
            actor=actor,
            action=action,
            target=target,
            details={"outcome": "success", **details},
            at=at,
        )
    )


async def settle_merged(
    session: AsyncSession, domain_id: str, proposal_id: str, *, revision: bytes, at: datetime
) -> list[str]:
    """Resolve the candidates waiting for `proposal_id` (module doc); returns their ids."""
    repo = ReconciliationCandidateRepository(session, domain_id)
    settled: list[str] = []
    for row in await repo.pending_for(proposal_id):
        pending = dict(row.decision or {})
        decided_by = row.decided_by or "unknown"
        decision = {k: v for k, v in pending.items() if k not in PENDING_MARKS}
        decision.update(proposal=proposal_id, revision=hex(revision))
        await repo.resolve(
            row.id,
            decision=decision,
            reason=str(pending.get("reason") or "merged"),
            by=decided_by,
            at=at,
        )
        await audit(
            session,
            domain_id,
            actor=decided_by,
            action=RESOLVE_ACTION,
            target=row.id,
            at=at,
            choice=pending.get("choice"),
            proposal=proposal_id,
            revision=hex(revision),
            via="proposal.merge",
        )
        settled.append(row.id)
    return settled
