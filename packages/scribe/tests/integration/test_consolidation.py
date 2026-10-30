"""Consolidation of resolution misses into vocabulary suggestions (design 8.10) over the
`supply` example: links to near values and names, collisions, sensitive Attributes, repeated
passes, retention and the optional model step. Also the coverage statistics of the default
branch head (design 8.11)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import Schema
from lorebase_inference.interfaces import ChatOptions, ChatResult, Message, Usage
from lorebase_scribe.consolidation import ConsolidationPass
from lorebase_scribe.settings import ConsolidationLlmSettings, ConsolidationSettings
from lorebase_store import coverage, values
from lorebase_store.repositories import SchemaSuggestionRepository, SuggestionRow, scope_to_domain
from lorebase_store.resolve_misses import MissSighting, record_misses
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import CoverageSettings, StoreSettings

from .support import DOMAIN, Clock, load_domain, rows, scalar, supply_schema

pytestmark = pytest.mark.integration


@pytest.fixture
def enabled() -> StoreRuntime:
    return StoreRuntime.from_settings(StoreSettings(resolve_misses={"enabled": True}))


@pytest.fixture
async def schema(
    sessions: async_sessionmaker[AsyncSession], runtime: StoreRuntime, clock: Clock
) -> Schema:
    schema = supply_schema()
    await load_domain(
        sessions, runtime, clock, schema, kinds={"Supplier": "supplier.csv", "Part": "part.csv"}
    )
    await values.run_value_index(sessions, DOMAIN, settings=runtime.settings)
    return schema


async def miss(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    phrase: str,
    misses: int,
    *,
    callers: Sequence[str] = ("c1",),
    seen: datetime,
) -> None:
    async with sessions() as session, session.begin():
        await record_misses(
            session,
            [MissSighting(DOMAIN, "main", phrase, misses, tuple(callers), seen, seen)],
            settings=runtime.settings.resolve_misses,
        )


async def stored(sessions: async_sessionmaker[AsyncSession]) -> dict[str, SuggestionRow]:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = await SchemaSuggestionRepository(session, DOMAIN).page(limit=500)
    return {f"{r.suggestion_type}:{r.name}": r for r in found}


def attribute_id(schema: Schema, kind: str, name: str) -> str:
    owner = schema.kind_by_api_name(kind)
    assert owner is not None
    found = owner.attribute_by_api_name(name)
    assert found is not None
    return found.id


async def test_misses_become_vocabulary_suggestions(
    sessions: async_sessionmaker[AsyncSession],
    enabled: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    now = clock()
    await miss(sessions, enabled, "terminatd", 4, callers=("c1", "c2"), seen=now)
    await miss(sessions, enabled, "plantz", 3, seen=now)
    await miss(sessions, enabled, "active", 9, seen=now)  # an indexed value: a collision
    # Nearest to the sensitive vat_number, which is never a target: part_number is next.
    await miss(sessions, enabled, "vat numbr", 5, seen=now)
    await miss(sessions, enabled, "zebra crossing", 5, seen=now)  # near nothing
    await miss(sessions, enabled, "terminatt", 1, seen=now)  # below min_misses
    settings = ConsolidationSettings(min_misses=2)
    first = await ConsolidationPass(sessions, enabled, settings, clock=clock).run(DOMAIN)
    assert (first.phrases, first.linked, first.created) == (5, 3, 3), first.as_dict()
    assert first.discarded == {"collision": 1, "unlinked": 1}
    found = await stored(sessions)
    alias = found["value_alias:terminatd"]
    supplier = schema.kind_by_api_name("Supplier")
    assert supplier is not None
    assert alias.kind_ref == supplier.id
    assert alias.target_ref == attribute_id(schema, "Supplier", "status")
    assert alias.payload == {"value": "terminated"}
    assert (alias.support, alias.documents, alias.sources) == (4, 1, ["resolve_misses"])
    assert alias.examples[0]["phrase"] == "terminatd"
    assert alias.examples[0]["callers"] == 2
    plant = schema.kind_by_api_name("Plant")
    assert plant is not None
    assert found["kind_alias:plantz"].kind_ref == plant.id
    assert not any(
        row.target_ref == attribute_id(schema, "Supplier", "vat_number") for row in found.values()
    )

    # A later pass replaces the phrase's share: support follows the misses, not the passes.
    await miss(sessions, enabled, "terminatd", 2, seen=clock())
    second = await ConsolidationPass(sessions, enabled, settings, clock=clock).run(DOMAIN)
    assert (second.created, second.updated) == (0, 3)
    assert (await stored(sessions))["value_alias:terminatd"].support == 6


async def test_misses_expire_and_nothing_is_proposed_when_recording_is_off(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    enabled: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    now = clock()
    await miss(sessions, enabled, "terminatd", 4, seen=now - timedelta(days=31))
    await miss(sessions, enabled, "plantz", 4, seen=now)
    off = await ConsolidationPass(sessions, runtime, ConsolidationSettings(), clock=clock).run(
        DOMAIN
    )
    assert (off.pruned, off.skipped) == (1, "store.resolve_misses is off")
    assert await stored(sessions) == {}
    left = await scalar(sessions, "SELECT array_agg(phrase) FROM resolve_misses")
    assert left == ["plantz"]


async def test_domain_cap_drops_new_phrases_but_keeps_counting_known_ones(
    sessions: async_sessionmaker[AsyncSession], clock: Clock, schema: Schema
) -> None:
    capped = StoreRuntime.from_settings(
        StoreSettings(resolve_misses={"enabled": True, "max_per_domain": 1, "max_callers": 2})
    )
    now = clock()
    await miss(sessions, capped, "plantz", 1, callers=("a",), seen=now)
    await miss(sessions, capped, "other", 1, seen=now)
    await miss(sessions, capped, "plantz", 2, callers=("b", "c"), seen=now)
    found = await scalar(
        sessions, "SELECT json_agg(json_build_array(phrase, misses, callers)) FROM resolve_misses"
    )
    assert found == [["plantz", 3, ["a", "b"]]]


class Describer:
    """A chat model answering with a fixed description and evidence."""

    def __init__(self, evidence: list[str]) -> None:
        self.evidence = evidence
        self.prompts: list[str] = []

    async def complete_json(
        self, messages: Sequence[Message], schema: Any, options: ChatOptions | None = None
    ) -> ChatResult:
        self.prompts.append(messages[-1].content)
        data = {"description": "Lifecycle state of the supplier.", "evidence": self.evidence}
        return ChatResult(data, Usage(100, 20), "fake-model")


@pytest.mark.parametrize(("evidence", "described"), [(["terminatd"], 1), (["something else"], 0)])
async def test_model_step_must_cite_missed_phrases(
    sessions: async_sessionmaker[AsyncSession],
    enabled: StoreRuntime,
    clock: Clock,
    schema: Schema,
    evidence: list[str],
    described: int,
) -> None:
    await miss(sessions, enabled, "terminatd", 12, seen=clock())
    model = Describer(evidence)
    settings = ConsolidationSettings(min_misses=2, llm=ConsolidationLlmSettings(enabled=True))
    report = await ConsolidationPass(
        sessions,
        enabled,
        settings,
        clock=clock,
        chat=lambda: model,  # type: ignore[arg-type,return-value]
    ).run(DOMAIN)
    assert report.described == described
    assert "terminatd" in model.prompts[0]
    assert "terminated" not in model.prompts[0].replace("terminatd", "")  # no values
    found = (await stored(sessions)).get("attribute_description:status")
    if described:
        assert found is not None
        assert found.payload == {"description": "Lifecycle state of the supplier."}
        assert found.examples[0]["phrases"] == ["terminatd"]
    else:
        assert found is None
        assert report.discarded["llm_unverified"] == 1


async def test_coverage_counts_records_and_missing_values(
    sessions: async_sessionmaker[AsyncSession], runtime: StoreRuntime, clock: Clock, schema: Schema
) -> None:
    settings = CoverageSettings()
    first = await coverage.run_coverage(sessions, DOMAIN, settings=settings, clock=clock)
    assert first.skipped is None
    assert len(first.counted) == len(schema.kinds)
    again = await coverage.run_coverage(sessions, DOMAIN, settings=settings, clock=clock)
    assert again.skipped == "unchanged"
    supplier = schema.kind_by_api_name("Supplier")
    plant = schema.kind_by_api_name("Plant")
    assert supplier is not None
    assert plant is not None
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        stats = await coverage.read_coverage(session, DOMAIN, "main", [supplier.id, plant.id])
    expected = rows("supplier.csv")
    assert stats[supplier.id].records == len(expected)
    for attribute in supplier.attributes.values():
        if attribute.is_derived:
            continue
        blank = sum(1 for row in expected if not row.get(attribute.api_name))
        assert stats[supplier.id].missing[attribute.id] == blank, attribute.api_name
    assert stats[plant.id].records == 0
    sampled = await coverage.run_coverage(
        sessions,
        DOMAIN,
        settings=CoverageSettings(max_records_per_kind=10),
        clock=clock,
        force=True,
    )
    assert sampled.counted
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        stats = await coverage.read_coverage(session, DOMAIN, "main", [supplier.id])
    assert stats[supplier.id].sampled
    assert stats[supplier.id].records == len(expected)
