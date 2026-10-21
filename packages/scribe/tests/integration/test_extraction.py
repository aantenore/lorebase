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
from lorebase_store.models.enums import ItemDecision, ProposalOrigin, ProposalStatus, RunStatus
from lorebase_store.repositories import (
    ExtractionJobRepository,
    ItemRow,
    MaterializedRepository,
    ProposalRepository,
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
