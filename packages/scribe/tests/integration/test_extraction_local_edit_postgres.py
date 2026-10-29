"""Extraction holds a value a person set last (design 9.1 "Extraction", 9.2 "Local edits"):
the item stays pending with hold reason `local_edit`, whatever the mode or confidence, while
a value last set by a Source or an import is accepted under the same settings.

The Domain is the `supply` example without C-3025; the amendment of C-3013 states the
Contract's values, so a Contract whose stored value differs yields a value item.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import Schema
from lorebase_core.revision import Author, Origin
from lorebase_inference import InferenceSettings
from lorebase_scribe.extraction.pipeline import ExtractionRun
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_store.commit import commit
from lorebase_store.models.enums import ItemDecision, ProposalStatus
from lorebase_store.runtime import StoreRuntime

from .extraction_support import proposal_items, seed_supply
from .support import DOMAIN, Clock, commit_on, record_change, rows, supply_schema
from .test_extraction import branch_record, extract, find
from .test_ingestion_modes import proposal_status, set_extraction

pytestmark = pytest.mark.integration

AMENDMENT = "amendment-c-3013-wexrix-filtration.md"
KEY = "C-3013"


@pytest.fixture
async def schema(
    sessions: async_sessionmaker[AsyncSession], runtime: StoreRuntime, clock: Clock
) -> Schema:
    schema = supply_schema()
    await seed_supply(sessions, runtime, clock, schema)
    return schema


def attribute_id(schema: Schema, name: str) -> str:
    kind = schema.kind_by_api_name("Contract")
    assert kind is not None
    found = kind.attribute_by_api_name(name)
    assert found is not None
    return found.id


async def set_contract(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
    origin: Origin,
    **values: str,
) -> None:
    kind = schema.kind_by_api_name("Contract")
    assert kind is not None
    original = next(r for r in rows("contract.csv") if r["contract_number"] == KEY)
    change = record_change(kind, {**original, **values})
    if origin.type == "operation":  # what a person does
        await commit_on(sessions, runtime, clock, [change])
        return
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            [change],
            author=Author(principal="svc_scribe"),
            message="Upstream change",
            origin=origin,
            runtime=runtime,
            clock=clock,
        )


def direct_run(
    sessions: async_sessionmaker[AsyncSession], runtime: StoreRuntime, clock: Clock
) -> ExtractionRun:
    # Everything the tier-0 rules find is over the threshold; the installation lets the
    # Domain loosen it.
    return ExtractionRun(
        sessions,
        runtime,
        ExtractionSettings(
            tiers=(0,),
            auto_accept_threshold=0.0,
            domain_may_loosen=("mode", "auto_accept_threshold"),
        ),
        InferenceSettings(),
        principal="svc_scribe",
        clock=clock,
    )


async def test_a_value_a_person_set_stays_pending_and_a_source_value_does_not(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> None:
    annual, incoterm = (attribute_id(schema, n) for n in ("annual_value", "incoterm"))
    kind = schema.kind_by_api_name("Contract")
    assert kind is not None
    # A person corrects the annual value; an import changes the Incoterm.
    await set_contract(
        sessions, runtime, clock, schema, Origin(type="operation"), annual_value="1000000.00"
    )
    await set_contract(
        sessions,
        runtime,
        clock,
        schema,
        Origin(type="import"),
        annual_value="1000000.00",
        incoterm="FCA",
    )
    # The import re-sent the person's value unchanged, which does not make it the importer's
    # (a Source writes only what changed); the Incoterm is the import's.
    await set_extraction(sessions, {"mode": "direct", "auto_accept_threshold": 0.0})

    report = await extract(direct_run(sessions, runtime, clock), sessions, AMENDMENT)

    assert report.mode == "direct"
    assert report.stats["held_local_edit"] >= 1
    assert report.proposal_id is not None
    items = await proposal_items(sessions, report.proposal_id)
    held = find(items, "value", key=KEY, attribute=annual)
    assert held.decision is ItemDecision.PENDING
    assert held.match is not None
    assert held.match["hold"] == "local_edit"
    free = find(items, "value", key=KEY, attribute=incoterm)
    assert free.decision is ItemDecision.ACCEPTED
    # Direct mode waits for the held item: nothing merged, the person's value stands.
    assert (report.direct_blocked, report.direct_merged) == ("pending_items", False)
    assert await proposal_status(sessions, report) is ProposalStatus.OPEN
    current = await branch_record(sessions, runtime, "main", kind.id, KEY)
    assert current is not None
    assert current.values[annual] == "1000000.00"
    assert current.values[incoterm] == "FCA"
