"""Reviewer decisions on extraction items (design 9.2 step 12, 12.3 Review).

`decide_items` records accept, reject or reset decisions (with optional edits) on items of an
open extraction Proposal, re-materializes the extraction branch so it holds exactly the
accepted items, and refreshes the Proposal (summary, checks, merge preview), all in the
caller's transaction. When the branch rejects the accepted set (for example a new Record that
misses a required value), the commit raises `ValidationFailedError` and the caller's
transaction must roll back: decisions and branch stay as they were.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.merge import MergePolicy
from lorebase_store.commit.schema import load_compiled
from lorebase_store.extraction.items import ItemEditError, normalize_edits
from lorebase_store.extraction.materialize import (
    MaterializeContext,
    Materialized,
    materialize_items,
)
from lorebase_store.extraction.proposal import refresh_proposal
from lorebase_store.merging import AttemptResult
from lorebase_store.models.enums import ItemDecision, ProposalOrigin, ProposalStatus
from lorebase_store.repositories.documents import DocumentRepository, ExtractionJobRepository
from lorebase_store.repositories.materialized import MaterializedRepository
from lorebase_store.repositories.proposal_items import ItemRow, ProposalItemRepository
from lorebase_store.repositories.proposals import ProposalRepository, ProposalRow
from lorebase_store.repositories.scope import scope_to_domain
from lorebase_store.runtime import StoreRuntime

MERGE_POLICY_KEY = "merge_policy"
"""Key of the merge policy in a Proposal's `policies`."""
UNKNOWN_PROMPT = "unknown"


class ReviewError(Exception):
    """A decision request that cannot be applied; `reason` names the case."""

    def __init__(self, reason: str, message: str, item_ids: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.reason = reason
        self.item_ids = list(item_ids)


@dataclass(frozen=True, slots=True)
class Decision:
    """One decision; `edits` of `None` keeps the item's stored edits, `{}` clears them."""

    item_id: str
    decision: ItemDecision
    edits: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class Reviewed:
    proposal: ProposalRow
    items: list[ItemRow]
    outcome: Materialized
    attempt: AttemptResult | None


async def extraction_proposal(
    session: AsyncSession, domain_id: str, proposal_id: str, *, for_update: bool = False
) -> ProposalRow:
    """The extraction Proposal, or `ReviewError` (`not_found`, `not_extraction`)."""
    proposal = await ProposalRepository(session, domain_id).get(proposal_id, for_update=for_update)
    if proposal is None:
        raise ReviewError("not_found", f"Proposal {proposal_id!r} not found.")
    if proposal.origin is not ProposalOrigin.EXTRACTION:
        raise ReviewError("not_extraction", f"Proposal {proposal_id!r} has no extraction items.")
    return proposal


async def decide_items(
    session: AsyncSession,
    domain_id: str,
    proposal_id: str,
    decisions: Sequence[Decision],
    *,
    actor: str,
    runtime: StoreRuntime,
    clock: Callable[[], datetime],
) -> Reviewed:
    """Apply `decisions`, re-materialize the extraction branch and refresh the Proposal.

    Raises `ReviewError` (`not_found`, `not_extraction`, `not_open`, `unknown_items`,
    `invalid_edits`), `ValidationFailedError` and the commit pipeline's other errors.
    """
    await scope_to_domain(session, domain_id)
    proposal = await extraction_proposal(session, domain_id, proposal_id, for_update=True)
    if proposal.status is not ProposalStatus.OPEN:
        raise ReviewError("not_open", f"Proposal {proposal_id!r} is {proposal.status.value}.")
    writes = runtime.settings.writes
    compiled = await MaterializedRepository(
        session, domain_id, proposal.target_ref, writes=writes
    ).schema()
    if compiled is None:
        raise ReviewError("not_found", f"branch {proposal.target_ref!r} is not materialized")
    schema = load_compiled(compiled.compiled)
    repository = ProposalItemRepository(session, domain_id)
    stored = {item.item_id: item for item in await repository.all(proposal_id, for_update=True)}
    unknown = sorted({d.item_id for d in decisions} - stored.keys())
    if unknown:
        raise ReviewError("unknown_items", "Items not in the Proposal.", unknown)
    now = clock()
    changed: list[str] = []
    for decision in decisions:
        item = stored[decision.item_id]
        try:
            edits = (
                item.edits
                if decision.edits is None
                else normalize_edits(item, decision.edits, schema) or None
            )
        except ItemEditError as error:
            raise ReviewError("invalid_edits", str(error), [error.item_id]) from None
        pending = decision.decision is ItemDecision.PENDING
        await repository.decide(
            proposal_id,
            item.item_id,
            decision=decision.decision,
            decided_by=None if pending else actor,
            decided_at=None if pending else now,
            edits=edits,
        )
        changed.append(item.item_id)
    items = await repository.all(proposal_id)
    job = await ExtractionJobRepository(session, domain_id).by_proposal(proposal_id)
    context = MaterializeContext(
        domain_id=domain_id,
        branch=proposal.source_ref,
        target=proposal.target_ref,
        job_id=job.id if job is not None else proposal.source_ref,
        prompt_version=job.prompt_version if job is not None else UNKNOWN_PROMPT,
        actor=actor,
        runtime=runtime,
        clock=clock,
    )
    outcome = await materialize_items(
        session,
        context,
        items,
        schema,
        message=f"Review of {proposal_id}: {len(changed)} decision(s)",
    )
    documents: list[str] = []
    if job is not None:
        found = DocumentRepository(session, domain_id)
        for document_id in job.documents:
            row = await found.get(document_id)
            documents.append(row.filename if row is not None else document_id)
    try:
        policy = MergePolicy.model_validate((proposal.policies or {}).get(MERGE_POLICY_KEY) or {})
    except ValidationError:
        policy = MergePolicy()
    final, attempt = await refresh_proposal(
        session, context, proposal, items, schema, outcome, documents=documents, policy=policy
    )
    by_id = {item.item_id: item for item in items}
    return Reviewed(final, [by_id[i] for i in changed], outcome, attempt)
