"""Schema suggestions aggregated over the `supply` example documents, with the rules only."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import Schema
from lorebase_inference import InferenceError, InferenceSettings, InvalidOutputError
from lorebase_inference.interfaces import ChatOptions, ChatResult, Message, Usage
from lorebase_scribe.extraction.pipeline import ExtractionModels, ExtractionRun
from lorebase_scribe.extraction.settings import (
    ChunkSettings,
    DiscoverySettings,
    ExtractionSettings,
)
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


class _DiscoveryChat:
    """A chat model answering the dedicated discovery call, recording the task it was given."""

    def __init__(self) -> None:
        self.tasks: list[str | None] = []

    async def complete_json(
        self,
        messages: Sequence[Message],
        schema: dict[str, Any],
        options: ChatOptions | None = None,
    ) -> ChatResult:
        self.tasks.append(options.task if options else None)
        data = {
            "kinds": [{"name": "Incoterm", "example": "DAP", "evidence": "Delivery is DAP"}],
            "attributes": [
                {
                    "kind": "Contract",
                    "name": "payment days",
                    "value": "30",
                    "evidence": "payment is due 30 days after invoice",
                }
            ],
        }
        return ChatResult(data=data, usage=Usage(10, 5), model="fake:disc")


async def test_the_dedicated_discovery_pass_feeds_suggestions(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    document = await store_document(sessions, DOCUMENTS / "amendment-c-3013-wexrix-filtration.md")
    chat = _DiscoveryChat()

    def extraction(llm_pass: bool) -> ExtractionRun:
        return ExtractionRun(
            sessions,
            runtime,
            ExtractionSettings(tiers=(0,), discovery=DiscoverySettings(llm_pass=llm_pass)),
            InferenceSettings(),
            principal="svc_scribe",
            clock=clock,
            models=lambda tiers: ExtractionModels(
                discovery_chat=chat if llm_pass else None, discovery_model="fake:disc"
            ),
        )

    await extraction(False).run(DOMAIN, [document])
    assert chat.tasks == []
    assert "kind:Incoterm" not in await stored(sessions)

    report = await extraction(True).run(DOMAIN, [document])
    assert chat.tasks == ["schema_discovery"] * report.stats["discovery_calls"]
    assert report.usage["input_tokens"] >= 10
    found = await stored(sessions)
    assert found["kind:Incoterm"].samples == ["DAP"]
    assert found["kind:Incoterm"].sources == ["discovery:fake:disc"]
    payment = found["attribute:payment_days"]
    contract = schema.kind_by_api_name("Contract")
    assert contract is not None
    assert (payment.kind_ref, payment.samples) == (contract.id, ["30"])


class _Tier2Chat:
    """Tier 2 model: every answer is unusable, but costs a few tokens and is logged."""

    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def complete_json(self, *_args: Any, **_kwargs: Any) -> ChatResult:
        self.log.append("tier2")
        raise InvalidOutputError("unusable", usage=Usage(10, 0))


class _CostlyDiscovery:
    """Discovery model: empty proposals at a high token cost, or a provider failure."""

    def __init__(self, log: list[str], *, fail: bool = False) -> None:
        self.log, self.fail = log, fail
        self.options: list[ChatOptions | None] = []

    async def complete_json(
        self,
        messages: Sequence[Message],
        schema: dict[str, Any],
        options: ChatOptions | None = None,
    ) -> ChatResult:
        self.log.append("discovery")
        self.options.append(options)
        if self.fail:
            raise InferenceError("provider down")
        return ChatResult(data={"kinds": [], "attributes": []}, usage=Usage(500, 500), model="m")


def _two_tier_run(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    models: ExtractionModels,
    *,
    job_tokens: int,
    discovery_tokens: int,
) -> ExtractionRun:
    return ExtractionRun(
        sessions,
        runtime,
        ExtractionSettings(
            tiers=(0, 2),
            max_tokens_per_job=job_tokens,
            chunking=ChunkSettings(max_chars=400, overlap_chars=50),
            discovery=DiscoverySettings(llm_pass=True, llm_max_tokens=discovery_tokens),
            cache=False,
        ),
        InferenceSettings(),
        principal="svc_scribe",
        clock=clock,
        models=lambda tiers: models,
    )


async def test_the_discovery_pass_runs_after_tier_2_and_cannot_starve_it(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    documents = [await store_document(sessions, p) for p in sorted(DOCUMENTS.glob("*.md"))]
    log: list[str] = []
    discovery = _CostlyDiscovery(log)
    models = ExtractionModels(
        chat=_Tier2Chat(log),
        chat_model="t:2",
        discovery_chat=discovery,
        discovery_model="t:d",
    )
    run = _two_tier_run(sessions, runtime, clock, models, job_tokens=1000, discovery_tokens=2500)
    report = await run.run(DOMAIN, documents)

    chunks = report.stats["chunks"]
    # Each call of the discovery pass costs 1000 tokens, as much as the whole tier 2 budget,
    # yet every chunk reaches tier 2: the passes do not share a budget.
    assert report.stats["model_calls"] == chunks
    assert "chunks_over_budget" not in report.stats
    assert report.stats["discovery_calls"] <= 3
    assert report.stats["discovery_chunks_over_budget"] > 0
    assert report.stats["discovery_tokens"] <= 2500 + 1000
    # Within one document, tier 2 comes before the discovery calls.
    first_discovery = log.index("discovery")
    assert log[:first_discovery].count("tier2") >= 1
    assert "tier2" in log[first_discovery:]  # later documents still get their tier 2
    # The answer is capped to what is left of the discovery budget.
    assert all(o is not None and o.max_tokens for o in discovery.options)
    assert max(o.max_tokens or 0 for o in discovery.options if o) <= 2500


async def test_a_failing_discovery_provider_costs_the_chunk_not_the_job(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    document = await store_document(sessions, DOCUMENTS / "quality-notice-p-10016.md")
    log: list[str] = []
    models = ExtractionModels(
        chat=_Tier2Chat(log),
        chat_model="t:2",
        discovery_chat=_CostlyDiscovery(log, fail=True),
        discovery_model="t:d",
    )
    run = _two_tier_run(sessions, runtime, clock, models, job_tokens=0, discovery_tokens=0)
    report = await run.run(DOMAIN, [document])
    assert report.stats["discovery_errors"] == report.stats["chunks"]
    assert "discovery_calls" not in report.stats
    assert any("provider down" in w for w in report.warnings)
    assert report.stats["model_calls"] == report.stats["chunks"]  # tier 2 was unaffected
