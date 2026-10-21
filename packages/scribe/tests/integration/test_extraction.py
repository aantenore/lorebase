"""Document extraction into Proposals on the `supply` example, with deterministic models.

The Domain holds Suppliers, Parts, Plants and Contracts without C-3025 and without
`supplied_by` Links, so the four example documents propose new facts about existing Records
and one new Contract.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import Schema
from lorebase_inference import InferenceSettings
from lorebase_scribe.extraction.pipeline import ExtractionReport, ExtractionRun
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_store.commit import ValidationFailedError
from lorebase_store.extraction import Decision, decide_items
from lorebase_store.merging import MergeRequest, MergeService
from lorebase_store.models.enums import ItemDecision, ProposalOrigin, ProposalStatus, RunStatus
from lorebase_store.repositories import (
    ExtractionJobRepository,
    ItemRow,
    MaterializedRepository,
    ProposalRepository,
    ProvenanceRepository,
    RecordRow,
    scope_to_domain,
)
from lorebase_store.runtime import StoreRuntime

from .extraction_support import (
    DOCUMENTS,
    NEW_CONTRACT,
    proposal_items,
    seed_supply,
    store_document,
)
from .support import DOMAIN, Clock, supply_schema

pytestmark = pytest.mark.integration

REVIEW = "supplier-review-2025-q4.md"
CONTRACT = "contract-c-3025-kavvik-filtration.md"
THRESHOLD = 0.55
"""Accepts the heading-section Links of the review (0.57) at once, nothing weaker."""


def ids(schema: Schema) -> dict[str, str]:
    """Kind and Relation api names to ids, and `Kind.attribute` to Attribute ids."""
    found: dict[str, str] = {}
    for kind in schema.kinds.values():
        found[kind.api_name] = kind.id
        for attribute in kind.attributes.values():
            found[f"{kind.api_name}.{attribute.api_name}"] = attribute.id
    for relation in schema.relations.values():
        found[relation.api_name] = relation.id
    return found


def extraction(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    *,
    tiers: Sequence[int] = (0,),
    inference: InferenceSettings | None = None,
) -> ExtractionRun:
    return ExtractionRun(
        sessions,
        runtime,
        ExtractionSettings(tiers=tuple(tiers), auto_accept_threshold=THRESHOLD),  # type: ignore[arg-type]
        inference or InferenceSettings(),
        principal="svc_scribe",
        clock=clock,
    )


async def extract(
    run: ExtractionRun, sessions: async_sessionmaker[AsyncSession], name: str
) -> ExtractionReport:
    document = await store_document(sessions, DOCUMENTS / name)
    return await run.run(DOMAIN, [document], requested_by="usr_editor")


async def branch_record(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    branch: str,
    kind_id: str,
    key: str,
) -> RecordRow | None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        rows = MaterializedRepository(session, DOMAIN, branch, writes=runtime.settings.writes)
        return (await rows.records(kind_id, [key])).get(key)


async def branch_links(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    branch: str,
    relation_id: str,
    pairs: list[tuple[str, str]],
) -> set[tuple[str, str]]:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        rows = MaterializedRepository(session, DOMAIN, branch, writes=runtime.settings.writes)
        return set(await rows.links(relation_id, pairs))


def find(items: list[ItemRow], item_type: str, **subject: Any) -> ItemRow:
    matches = [
        item
        for item in items
        if item.item_type == item_type and all(item.subject.get(k) == v for k, v in subject.items())
    ]
    assert len(matches) == 1, (item_type, subject, [i.subject for i in items])
    return matches[0]


async def decide(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    proposal_id: str,
    decisions: list[Decision],
) -> None:
    async with sessions() as session, session.begin():
        await decide_items(
            session,
            DOMAIN,
            proposal_id,
            decisions,
            actor="usr_reviewer",
            runtime=runtime,
            clock=clock,
        )


@pytest.fixture
async def schema(
    sessions: async_sessionmaker[AsyncSession], runtime: StoreRuntime, clock: Clock
) -> Schema:
    schema = supply_schema()
    await seed_supply(sessions, runtime, clock, schema)
    return schema


async def test_review_document_proposes_links_between_existing_records(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    names = ids(schema)
    report = await extract(extraction(sessions, runtime, clock), sessions, REVIEW)
    assert report.proposal_id is not None
    assert report.branch == f"extract/{report.job_id}"
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        proposal = await ProposalRepository(session, DOMAIN).get(report.proposal_id)
        job = await ExtractionJobRepository(session, DOMAIN).get(report.job_id)
    assert proposal is not None
    assert job is not None
    assert (proposal.origin, proposal.status) == (ProposalOrigin.EXTRACTION, ProposalStatus.OPEN)
    assert (proposal.source_ref, proposal.target_ref) == (report.branch, "main")
    assert proposal.checks is not None
    assert proposal.checks["extraction"]["items"] == report.items
    assert proposal.checks["merge"]["open_conflicts"] == 0
    assert (job.status, job.proposal_id, job.created_by) == (
        RunStatus.SUCCEEDED,
        proposal.id,
        "usr_editor",
    )

    items = await proposal_items(sessions, report.proposal_id)
    for item in items:
        assert item.evidence is not None
        assert item.evidence["quote"]
        assert item.confidence is not None
        assert item.job_id == report.job_id
        assert item.tier == 0
    supplied = find(
        items, "link", relation=names["supplied_by"], **{"from": "P-10012", "to": "S-1025"}
    )
    assert supplied.decision is ItemDecision.ACCEPTED  # endpoints exist, confidence 0.57
    assert supplied.match is not None
    assert supplied.match["from"]["status"] == "matched"
    assert "Kavvik" in supplied.evidence["quote"] or "P-10012" in supplied.evidence["quote"]
    contract = find(items, "record", kind=names["Contract"], key=NEW_CONTRACT)
    assert contract.decision is ItemDecision.PENDING  # title and start date are missing
    assert contract.match is not None
    assert contract.match["status"] == "new"
    assert set(contract.match["missing"]) == {"start_date", "title"}

    pairs = [("P-10012", "S-1025"), ("P-10018", "S-1025"), ("P-10023", "S-1025")]
    on_branch = await branch_links(sessions, runtime, report.branch, names["supplied_by"], pairs)
    assert on_branch == set(pairs)
    assert not await branch_links(sessions, runtime, "main", names["supplied_by"], pairs)


async def test_rejecting_an_item_reverts_it_on_the_branch(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    names = ids(schema)
    report = await extract(extraction(sessions, runtime, clock), sessions, REVIEW)
    assert report.proposal_id is not None
    items = await proposal_items(sessions, report.proposal_id)
    rejected = find(
        items, "link", relation=names["supplied_by"], **{"from": "P-10023", "to": "S-1025"}
    )
    await decide(
        sessions,
        runtime,
        clock,
        report.proposal_id,
        [Decision(rejected.item_id, ItemDecision.REJECTED)],
    )
    after = {i.item_id: i for i in await proposal_items(sessions, report.proposal_id)}
    assert after[rejected.item_id].decision is ItemDecision.REJECTED
    assert after[rejected.item_id].decided_by == "usr_reviewer"
    pairs = [("P-10012", "S-1025"), ("P-10023", "S-1025")]
    assert await branch_links(sessions, runtime, report.branch, names["supplied_by"], pairs) == {
        ("P-10012", "S-1025")
    }
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        proposal = await ProposalRepository(session, DOMAIN).get(report.proposal_id)
    assert proposal is not None
    assert proposal.checks is not None
    assert proposal.checks["extraction"]["decisions"]["rejected"] == 1


async def test_accepting_the_new_contract_commits_it_with_evidence_lineage(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    names = ids(schema)
    report = await extract(extraction(sessions, runtime, clock), sessions, CONTRACT)
    assert report.proposal_id is not None
    items = await proposal_items(sessions, report.proposal_id)
    record = find(items, "record", kind=names["Contract"], key=NEW_CONTRACT)
    values = record.subject["values"]
    assert values[names["Contract.start_date"]] == "2024-02-08"
    assert values[names["Contract.end_date"]] == "2026-02-07"
    assert values[names["Contract.annual_value"]] == "1596000.00"
    assert values[names["Contract.incoterm"]] == "DDP"
    assert values[names["Contract.title"]] == "Framework Supply Agreement C-3025"
    # Complete with the `with_supplier` Link proposed next to it, but its values are rule
    # guesses (confidence below the threshold): both wait for review.
    assert record.match is not None
    assert record.match["complete"] is True
    assert record.confidence is not None
    assert record.confidence < THRESHOLD
    link = find(
        items, "link", relation=names["with_supplier"], **{"from": NEW_CONTRACT, "to": "S-1025"}
    )
    assert record.decision is link.decision is ItemDecision.PENDING

    # The Contract alone breaks `with_supplier` (required): nothing is written.
    with pytest.raises(ValidationFailedError):
        await decide(
            sessions,
            runtime,
            clock,
            report.proposal_id,
            [Decision(record.item_id, ItemDecision.ACCEPTED)],
        )
    contract_kind = names["Contract"]
    assert (
        await branch_record(sessions, runtime, report.branch, contract_kind, NEW_CONTRACT) is None
    )

    await decide(
        sessions,
        runtime,
        clock,
        report.proposal_id,
        [
            Decision(record.item_id, ItemDecision.ACCEPTED, {"values": {"status": "active"}}),
            Decision(link.item_id, ItemDecision.ACCEPTED),
        ],
    )
    stored = await branch_record(sessions, runtime, report.branch, contract_kind, NEW_CONTRACT)
    assert stored is not None
    assert stored.values[names["Contract.status"]] == "active"
    entry_id = stored.lineage[names["Contract.start_date"]]
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        entries = await ProvenanceRepository(
            session, DOMAIN, writes=runtime.settings.writes
        ).get_many([entry_id])
    origin = entries[entry_id].origin_ref
    assert entries[entry_id].origin_type == "extraction"
    assert origin["job_id"] == report.job_id
    assert origin["document_id"] == report.documents[0]
    assert origin["model_id"] == "rules"
    text = (DOCUMENTS / CONTRACT).read_text()
    assert "8 February 2024" in text[origin["span_start"] : origin["span_end"]]

    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        proposal = await ProposalRepository(session, DOMAIN).get(report.proposal_id)
        assert proposal is not None
        merged = await MergeService(session, DOMAIN, runtime=runtime, clock=clock).merge(
            MergeRequest(proposal=proposal, actor="usr_maintainer")
        )
    assert merged.merged
    assert await branch_record(sessions, runtime, "main", contract_kind, NEW_CONTRACT) is not None


async def test_every_example_document_is_extracted(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    run = extraction(sessions, runtime, clock)
    reports = {
        path.name: await extract(run, sessions, path.name)
        for path in sorted(DOCUMENTS.glob("*.md"))
    }
    assert len(reports) == 4
    for report in reports.values():
        assert report.stats["chunks"] >= 1
        assert report.stats["mentions"] >= 1
    # The amendment repeats what the Domain holds already: no Proposal.
    amendment = reports["amendment-c-3013-wexrix-filtration.md"]
    assert amendment.proposal_id is None
    assert amendment.stats.get("unchanged", 0) >= 1
    assert all(r.proposal_id for r in reports.values() if r is not amendment)


async def test_tier2_fake_model_is_called_once_per_chunk_and_cached(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    inference = InferenceSettings.model_validate(
        {"llm": {"provider": "fake"}, "local_extractors": {"provider": "fake"}}
    )
    run = extraction(sessions, runtime, clock, tiers=(0, 1, 2), inference=inference)
    first = await extract(run, sessions, CONTRACT)
    assert first.stats["model_calls"] == first.stats["chunks"]
    assert first.usage["input_tokens"] > 0
    second = await extract(run, sessions, CONTRACT)
    assert second.stats.get("model_calls", 0) == 0
    assert second.stats["cache_hits"] == first.stats["chunks"]
    assert second.items == first.items


@pytest.mark.skipif(os.environ.get("LOREBASE_LIVE_LLM") != "1", reason="set LOREBASE_LIVE_LLM=1")
async def test_live_model_extracts_the_contract(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    """Tier 2 against the configured OpenAI-compatible endpoint (default: local Ollama)."""
    inference = InferenceSettings.model_validate(
        {
            "llm": {
                "base_url": os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),
                "model": os.environ.get("LLM_MODEL", "qwen2.5:7b-instruct"),
            }
        }
    )
    run = extraction(sessions, runtime, clock, tiers=(0, 2), inference=inference)
    report = await extract(run, sessions, CONTRACT)
    assert report.stats["model_calls"] >= 1
    assert report.proposal_id is not None
