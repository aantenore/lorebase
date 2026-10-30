"""Consolidation of resolution misses into vocabulary suggestions (design 8.10) over the
`supply` example: links to near values and names, collisions, sensitive Attributes, repeated
passes, retention and the optional model step. Also the coverage statistics of the default
branch head (design 8.11)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.infer.suggestions import Observation, SuggestionOptions, SuggestionType
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
    settings = ConsolidationSettings(min_misses=2, min_callers=1)
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
    assert (alias.support, alias.documents, alias.sources) == (4, 0, ["resolve_misses"])
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


async def test_domain_cap_evicts_the_least_missed_and_bounds_each_caller(
    sessions: async_sessionmaker[AsyncSession], clock: Clock, schema: Schema
) -> None:
    capped = StoreRuntime.from_settings(
        StoreSettings(
            resolve_misses={
                "enabled": True,
                "max_per_domain": 2,
                "max_callers": 2,
                "max_phrases_per_caller": 1,
            }
        )
    )
    now = clock()
    await miss(sessions, capped, "plantz", 5, callers=("a", "b"), seen=now)
    await miss(sessions, capped, "plantz", 1, callers=("c",), seen=now)  # known: counts
    await miss(sessions, capped, "first junk", 1, callers=("spammer",), seen=now)
    await miss(sessions, capped, "second junk", 1, callers=("spammer",), seen=now)  # caller cap
    await miss(sessions, capped, "useful", 2, callers=("d",), seen=clock())  # evicts the junk
    await miss(sessions, capped, "noise", 1, callers=("e",), seen=clock())  # evicts nothing
    found = await scalar(
        sessions,
        "SELECT json_agg(json_build_array(phrase, misses, callers) ORDER BY phrase) "
        "FROM resolve_misses",
    )
    assert found == [["plantz", 6, ["a", "b"]], ["useful", 2, ["d"]]]


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


@pytest.mark.parametrize(("evidence", "described"), [(["tierr"], 1), (["something else"], 0)])
async def test_model_step_must_cite_missed_phrases(
    sessions: async_sessionmaker[AsyncSession],
    enabled: StoreRuntime,
    clock: Clock,
    schema: Schema,
    evidence: list[str],
    described: int,
) -> None:
    await miss(sessions, enabled, "tierr", 12, callers=("c1", "c2"), seen=clock())
    await miss(sessions, enabled, "terminatd", 12, callers=("c1", "c2"), seen=clock())
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
    assert len(model.prompts) == 1
    assert "tierr" in model.prompts[0]
    # A phrase linked to a value is a near-copy of a stored value: never sent.
    assert "terminatd" not in model.prompts[0]
    assert "terminated" not in model.prompts[0]
    found = (await stored(sessions)).get("attribute_description:tier")
    if described:
        assert found is not None
        assert found.payload == {"description": "Lifecycle state of the supplier."}
        assert found.examples[0]["phrases"] == ["tierr"]
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
    assert all(0 <= n <= len(expected) for n in stats[supplier.id].missing.values())
    first_unseen = stats[supplier.id].unseen_at
    refreshed = await coverage.run_coverage(sessions, DOMAIN, settings=settings, clock=clock)
    assert refreshed.skipped == "unchanged"
    assert set(refreshed.refreshed) == set(schema.kinds)
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        again_stats = await coverage.read_coverage(session, DOMAIN, "main", [supplier.id])
    assert first_unseen is not None
    assert again_stats[supplier.id].unseen_at is not None
    assert again_stats[supplier.id].unseen_at > first_unseen  # Records unseen are recounted
    assert again_stats[supplier.id].revision == stats[supplier.id].revision


async def suggestions_of(sessions: async_sessionmaker[AsyncSession]) -> dict[str, SuggestionRow]:
    return await stored(sessions)


async def test_pruned_phrases_leave_suggestions_and_purge_erases_them(
    sessions: async_sessionmaker[AsyncSession],
    enabled: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    """Design 8.10: no phrase outlives the retention period in a suggestion, decided or not."""
    now = clock()
    settings = ConsolidationSettings(min_misses=2, min_callers=1)
    await miss(sessions, enabled, "terminatd", 4, seen=now)
    await miss(sessions, enabled, "plantz", 3, seen=now)
    await ConsolidationPass(sessions, enabled, settings, clock=clock).run(DOMAIN)
    found = await stored(sessions)
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await SchemaSuggestionRepository(session, DOMAIN).decide(
            [found["kind_alias:plantz"].id],
            status="rejected",
            decided_by="usr_reviewer",
            decided_at=now,
        )
    later = Clock()
    later._now = now + timedelta(days=31)
    await miss(sessions, enabled, "terminatd", 1, seen=later())  # still missed: kept
    report = await ConsolidationPass(sessions, enabled, settings, clock=later).run(DOMAIN)
    assert report.pruned == 1
    assert report.removed == 1
    left = await stored(sessions)
    assert set(left) == {"value_alias:terminatd"}
    assert "plantz" not in str(left)

    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        assert await SchemaSuggestionRepository(session, DOMAIN).purge_vocabulary() == 1
    assert await stored(sessions) == {}


async def test_a_relinked_phrase_leaves_its_previous_pending_suggestion(
    sessions: async_sessionmaker[AsyncSession],
    enabled: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    plant = schema.kind_by_api_name("Plant")
    assert plant is not None
    stale = Observation(
        SuggestionType.KIND_ALIAS,
        "terminatd",
        "miss:main:terminatd",
        0.5,
        "resolve_misses",
        kind=plant.id,
        weight=4,
        evidence={"document_id": "miss:main:terminatd", "phrase": "terminatd"},
    )
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await SchemaSuggestionRepository(session, DOMAIN).record(
            [stale],
            job_id="old",
            options=SuggestionOptions(),
            now=clock(),
            new_id=lambda: "ssg_stale",
        )
    await miss(sessions, enabled, "terminatd", 4, seen=clock())
    settings = ConsolidationSettings(min_misses=2, min_callers=1)
    await ConsolidationPass(sessions, enabled, settings, clock=clock).run(DOMAIN)
    assert set(await stored(sessions)) == {"value_alias:terminatd"}


async def test_vocabulary_suggestions_cannot_crowd_out_extraction(
    sessions: async_sessionmaker[AsyncSession], clock: Clock, schema: Schema
) -> None:
    options = SuggestionOptions(max_tracked=2, max_vocabulary_tracked=1)
    plant = schema.kind_by_api_name("Plant")
    assert plant is not None

    def alias(phrase: str) -> Observation:
        return Observation(
            SuggestionType.KIND_ALIAS, phrase, f"miss:main:{phrase}", 0.5, "resolve_misses",
            kind=plant.id,
        )  # fmt: skip

    extracted = Observation(SuggestionType.KIND, "Quality notice", "doc_1", 0.9, "rules")
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        repository = SchemaSuggestionRepository(session, DOMAIN)
        counter = iter(range(100))
        vocabulary = await repository.record(
            [alias("a junk"), alias("b junk"), alias("c junk")],
            job_id="j",
            options=options,
            now=clock(),
            new_id=lambda: f"ssg_{next(counter)}",
        )
        extraction = await repository.record(
            [extracted],
            job_id="j",
            options=options,
            now=clock(),
            new_id=lambda: f"ssg_{next(counter)}",
        )
    assert (vocabulary.created, vocabulary.dropped) == (1, 2)
    assert extraction.created == 1


async def test_last_successful_run_is_one_index_probe(
    sessions: async_sessionmaker[AsyncSession], clock: Clock, schema: Schema
) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await session.execute(
            text(
                "INSERT INTO source_runs (domain_id, id, source_id, status, started_at, "
                "finished_at, stats) SELECT :d, 'run_' || g, 'src_x', "
                "CAST(CASE WHEN g % 3 = 0 THEN 'failed' ELSE 'succeeded' END AS run_status), "
                "now() - make_interval(mins => 5000 - g), now() - make_interval(mins => 5000 - g), "
                "CASE WHEN g = 4999 THEN '{\"dry_run\": true}'::jsonb ELSE '{}'::jsonb END "
                "FROM generate_series(1, 5000) AS g"
            ),
            {"d": DOMAIN},
        )
        await session.execute(text("ANALYZE source_runs"))
        last = await coverage.last_runs(session, DOMAIN, ["src_x", "src_none"])
        plan = "\n".join(
            str(r[0])
            for r in (
                await session.execute(
                    text(f"EXPLAIN {coverage._LAST_RUNS.text}"),
                    {"domain": DOMAIN, "sources": ["src_x"]},
                )
            ).all()
        )
    newest = await scalar(
        sessions,
        "SELECT max(finished_at) FROM source_runs WHERE source_id = 'src_x' "
        "AND status = 'succeeded' AND coalesce(stats ->> 'dry_run', 'false') <> 'true'",
    )
    assert last == {"src_x": newest, "src_none": None}
    assert "ix_source_runs_last_succeeded" in plan
