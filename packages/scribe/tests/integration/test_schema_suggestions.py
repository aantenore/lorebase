"""Schema suggestions aggregated over the `supply` example documents, with the rules only."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import Schema
from lorebase_inference import InferenceSettings
from lorebase_scribe.extraction.pipeline import ExtractionRun
from lorebase_scribe.extraction.settings import DiscoverySettings, ExtractionSettings
from lorebase_store.repositories import SchemaSuggestionRepository, SuggestionRow, scope_to_domain
from lorebase_store.runtime import StoreRuntime

from .extraction_support import DOCUMENTS, seed_supply, store_document
from .support import DOMAIN, Clock, supply_schema

pytestmark = pytest.mark.integration


def run(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    discovery: DiscoverySettings | None = None,
) -> ExtractionRun:
    return ExtractionRun(
        sessions,
        runtime,
        ExtractionSettings(tiers=(0,), discovery=discovery or DiscoverySettings()),
        InferenceSettings(),
        principal="svc_scribe",
        clock=clock,
    )


async def stored(sessions: async_sessionmaker[AsyncSession]) -> dict[str, SuggestionRow]:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        rows = await SchemaSuggestionRepository(session, DOMAIN).page(limit=500)
    return {f"{r.suggestion_type}:{r.name}": r for r in rows}


@pytest.fixture
async def schema(
    sessions: async_sessionmaker[AsyncSession], runtime: StoreRuntime, clock: Clock
) -> Schema:
    schema = supply_schema()
    await seed_supply(sessions, runtime, clock, schema)
    return schema


async def test_example_documents_aggregate_into_suggestions(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    extraction = run(sessions, runtime, clock)
    documents = {
        path.name: await store_document(sessions, path) for path in sorted(DOCUMENTS.glob("*.md"))
    }
    reports = {name: await extraction.run(DOMAIN, [doc]) for name, doc in documents.items()}
    found = await stored(sessions)

    notice = found["kind:QualityNotice"]
    assert (notice.support, notice.samples, notice.status) == (1, ["QN-2025-114"], "pending")
    assert notice.examples[0]["quote"] == "Quality notice QN-2025-114"
    assert notice.examples[0]["document_id"] == documents["quality-notice-p-10016.md"]
    part = schema.kind_by_api_name("Part")
    assert part is not None
    assert found["relation:part"].kind_ref == "+QualityNotice"
    assert found["relation:part"].target_ref == part.id

    ends = found["attribute:ends"]
    assert ends.support == 3
    assert ends.samples == ["2026-02-07", "2026-02-09", "2026-04-12"]
    assert ends.sources == ["rules"]
    assert len(ends.examples) == runtime.schema.suggestions.max_examples
    buyer = found["attribute:buyer"]
    contract = schema.kind_by_api_name("Contract")
    assert contract is not None
    assert buyer.kind_ref == contract.id
    assert "attribute:risk_score" not in found  # Supplier has it
    review = reports["supplier-review-2025-q4.md"]
    assert review.stats["suggestions_known"] >= 3
    assert review.stats["suggestion_observations"] >= 6

    # Extracting a document again replaces its share instead of adding to it.
    again = await extraction.run(DOMAIN, [documents["supplier-review-2025-q4.md"]])
    assert again.stats["suggestions_updated"] >= 1
    assert (await stored(sessions))["attribute:ends"].support == 3


async def test_discovery_can_be_turned_off(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    document = await store_document(sessions, DOCUMENTS / "quality-notice-p-10016.md")
    report = await run(sessions, runtime, clock, DiscoverySettings(enabled=False)).run(
        DOMAIN, [document]
    )
    assert "suggestion_observations" not in report.stats
    assert await stored(sessions) == {}
