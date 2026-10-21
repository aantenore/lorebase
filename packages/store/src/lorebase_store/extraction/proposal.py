"""Keep an extraction Proposal in step with its items: summary, checks and merge preview."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.merge import MergePolicy
from lorebase_core.model import Schema
from lorebase_store.extraction.items import proposal_text, summarize
from lorebase_store.extraction.materialize import MaterializeContext, Materialized
from lorebase_store.merging import AttemptResult, MergeRequest, MergeService
from lorebase_store.models.enums import ProposalStatus
from lorebase_store.repositories.proposal_items import ItemRow
from lorebase_store.repositories.proposals import ProposalRepository, ProposalRow

EXTRACTION_CHECK = "extraction"
"""Key of the item summary in `proposals.checks`."""


def extraction_check(
    items: Sequence[ItemRow], schema: Schema, outcome: Materialized
) -> dict[str, Any]:
    """The `extraction` entry of a Proposal's checks."""
    return {
        **summarize(items, schema),
        "applied": len(outcome.applied),
        "skipped": dict(outcome.skipped),
        "revision": None if outcome.revision is None else outcome.revision.hex(),
    }


async def refresh_proposal(
    session: AsyncSession,
    context: MaterializeContext,
    proposal: ProposalRow,
    items: Sequence[ItemRow],
    schema: Schema,
    outcome: Materialized,
    *,
    documents: Sequence[str],
    policy: MergePolicy | None = None,
) -> tuple[ProposalRow, AttemptResult | None]:
    """Rewrite the Proposal's title, description and `extraction` check from its items, then
    recompute the merge preview when the Proposal is open."""
    summary = summarize(items, schema)
    title, description = proposal_text(summary, documents)
    checks = dict(proposal.checks or {})
    checks[EXTRACTION_CHECK] = extraction_check(items, schema, outcome)
    proposals = ProposalRepository(session, context.domain_id)
    updated = await proposals.update(
        proposal.id, at=context.clock(), title=title, description=description, checks=checks
    )
    assert updated is not None
    if updated.status is not ProposalStatus.OPEN:
        return updated, None
    service = MergeService(session, context.domain_id, runtime=context.runtime, clock=context.clock)
    result = await service.recompute(
        MergeRequest(proposal=updated, actor=context.actor, policy=policy or MergePolicy())
    )
    final = await proposals.get(proposal.id)
    assert final is not None
    return final, result
