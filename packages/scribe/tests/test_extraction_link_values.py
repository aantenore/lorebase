"""Tier 2 link Attributes: schema, prompt, parsing and the Link items built from answers."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from lorebase_core.model import Schema
from lorebase_scribe.extraction.assemble import CurrentState, assemble_items
from lorebase_scribe.extraction.candidates import LinkCandidate, MatchCandidate
from lorebase_scribe.extraction.matching import Dictionary, Resolver
from lorebase_scribe.extraction.ontology import Ontology, build_ontology, fragment_schema
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_scribe.extraction.text import Chunk
from lorebase_scribe.extraction.tier2 import Tier2Result, build_messages, run_tier2
from lorebase_store.models.enums import ItemDecision
from lorebase_store.repositories.proposal_items import ItemRow

from .test_extraction_discovery import Answer, kind_id, observe

SETTINGS = ExtractionSettings()
TEXT = "Part P-10012 is stocked at plant PL-01 with 480 units on hand."
NOW = datetime(2026, 1, 1, tzinfo=UTC)


class Stored:
    """`RecordLookup` of a Domain that holds the Part and the Plant of the text."""

    def __init__(self, schema: Schema) -> None:
        self.records = {
            (kind_id(schema, "Part"), "P-10012"): "Temperature probe",
            (kind_id(schema, "Plant"), "PL-01"): "Rotterdam plant",
        }

    async def titles_of_keys(self, kind_id: str, keys: Sequence[str]) -> dict[str, str | None]:
        return {k: self.records[(kind_id, k)] for k in keys if (kind_id, k) in self.records}

    async def similar(
        self, text: str, kind_ids: Sequence[str], *, threshold: float, limit: int
    ) -> list[MatchCandidate]:
        return []


@pytest.fixture
def ontology(supply_schema: Schema) -> Ontology:
    return build_ontology(supply_schema, None)


def link_answer(values: dict[str, Any] | None) -> dict[str, Any]:
    link: dict[str, Any] = {
        "relation": "stocked_at",
        "from": "P-10012",
        "to": "PL-01",
        "evidence": "P-10012 is stocked at plant PL-01",
    }
    if values is not None:
        link["values"] = values
    return {"records": [], "links": [link]}


async def tier2(
    ontology: Ontology, schema: Schema, data: dict[str, Any]
) -> tuple[Tier2Result, Answer]:
    _, chunks, resolved = await observe(ontology, TEXT)
    chat = Answer(data)
    resolver = Resolver(ontology, SETTINGS, Stored(schema), Dictionary([], min_chars=4))
    result = await run_tier2(
        chat,
        ontology,
        chunks[0],
        resolved,
        resolver,
        document_id="doc_1",
        schema_hash=b"\0" * 32,
        model_key="fake:test",
        settings=SETTINGS,
        cache=None,
    )
    return result, chat


def assemble(ontology: Ontology, schema: Schema, link: LinkCandidate) -> ItemRow:
    part, plant = kind_id(schema, "Part"), kind_id(schema, "Plant")
    current = CurrentState(records={(part, "P-10012"): {}, (plant, "PL-01"): {}}, links=frozenset())
    assembled = assemble_items(
        ontology,
        [],
        [],
        [link],
        current,
        job_id="exj_1",
        principal="svc_scribe",
        now=NOW,
        settings=ExtractionSettings(auto_accept_threshold=0.1),
    )
    (item,) = assembled.items
    return item


def branches(schema: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = schema["properties"]["links"]["items"]["anyOf"]
    return found


def stocked_branch(schema: dict[str, Any]) -> dict[str, Any]:
    (branch,) = (
        b for b in branches(schema) if b["properties"]["relation"] == {"const": "stocked_at"}
    )
    return branch


def quantity_id(schema: Schema) -> str:
    found = schema.relation_by_api_name("stocked_at")
    assert found is not None
    relation = found[0]
    (attribute,) = (a for a in relation.attributes.values() if a.api_name == "quantity")
    return attribute.id


def test_schema_lists_the_required_link_attribute(ontology: Ontology) -> None:
    schema = fragment_schema(ontology, list(ontology.kinds))
    branch = stocked_branch(schema)
    values = branch["properties"]["values"]
    assert set(values["properties"]) == {"quantity"}
    assert values["properties"]["quantity"]["properties"]["value"]["type"] == "integer"
    assert values["required"] == ["quantity"]
    assert "values" in branch["required"]
    plain = [b for b in branches(schema) if "values" not in b["properties"]]
    assert plain
    assert "stocked_at" not in plain[0]["properties"]["relation"]["enum"]


def test_prompt_asks_for_required_link_attributes(ontology: Ontology) -> None:
    system, user = build_messages(ontology, list(ontology.kinds), Chunk(0, 0, 4, "text"), [])
    assert "link attribute quantity (required" in user.content
    assert "required ones" in system.content


async def test_answer_with_quantity_yields_a_link_carrying_it(
    ontology: Ontology, supply_schema: Schema
) -> None:
    result, chat = await tier2(
        ontology,
        supply_schema,
        link_answer({"quantity": {"value": 480, "evidence": "480 units on hand"}}),
    )
    (link,) = result.links
    assert link.values == {quantity_id(supply_schema): "480"}
    assert "values" in stocked_branch(chat.schemas[0])["properties"]
    item = assemble(ontology, supply_schema, link)
    assert item.subject["values"] == {quantity_id(supply_schema): "480"}


async def test_ungrounded_link_value_is_dropped(ontology: Ontology, supply_schema: Schema) -> None:
    result, _ = await tier2(
        ontology,
        supply_schema,
        link_answer({"quantity": {"value": 999, "evidence": "480 units on hand"}}),
    )
    (link,) = result.links
    assert link.values == {}
    assert result.discarded == {"ungrounded_value": 1}


async def test_answer_without_quantity_stays_pending_with_a_reason(
    ontology: Ontology, supply_schema: Schema
) -> None:
    result, _ = await tier2(ontology, supply_schema, link_answer(None))
    (link,) = result.links
    assert link.values == {}
    item = assemble(ontology, supply_schema, link)
    assert item.decision is ItemDecision.PENDING
    assert item.match is not None
    assert item.match["missing"] == ["quantity"]
