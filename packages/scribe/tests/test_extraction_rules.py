"""Mentions, entity resolution, values, the tier 2 schema and answers, item assembly."""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from lorebase_core.model import Schema
from lorebase_inference import InferenceSettings
from lorebase_inference.fake import FakeChatModel, prompt_hash
from lorebase_scribe.extraction.assemble import CurrentState, assemble_items
from lorebase_scribe.extraction.candidates import MatchCandidate, Mention, Resolved
from lorebase_scribe.extraction.matching import (
    Dictionary,
    Resolver,
    TitleEntry,
    disambiguate,
    non_overlapping,
    normalize_title,
    tier0_mentions,
)
from lorebase_scribe.extraction.ontology import (
    Ontology,
    build_ontology,
    fragment_schema,
    key_regex,
)
from lorebase_scribe.extraction.relations import cooccurrence_links, heading_links
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_scribe.extraction.text import Chunk, sentence_bounds
from lorebase_scribe.extraction.tier2 import build_messages, grounded, locate, run_tier2
from lorebase_scribe.extraction.values import sentence_values
from lorebase_store.extraction import proposal_text, summarize
from lorebase_store.models.enums import ItemDecision

SETTINGS = ExtractionSettings()
NOW = datetime(2026, 10, 21, tzinfo=UTC)

RECORDS = {
    ("Supplier", "S-1025"): "Kavvik Filtration A.S.",
    ("Supplier", "S-1013"): "Wexrix Filtration Sp. z o.o.",
    ("Part", "P-10023"): "Needle roller bearing NK25",
    ("Part", "P-10033"): "Needle roller bearing NK25",
    ("Part", "P-10012"): "Temperature probe PT100 150 mm",
    ("Contract", "C-3013"): "Framework supply agreement 2023 - Wexrix Filtration Sp. z o.o.",
}


class Lookup:
    """`RecordLookup` over RECORDS (Kind api names mapped to ids)."""

    def __init__(self, schema: Schema) -> None:
        self._records: dict[tuple[str, str], str] = {}
        for (kind_name, key), title in RECORDS.items():
            kind = schema.kind_by_api_name(kind_name)
            assert kind is not None
            self._records[(kind.id, key)] = title

    async def titles_of_keys(self, kind_id: str, keys: Sequence[str]) -> dict[str, str | None]:
        return {k: self._records[(kind_id, k)] for k in keys if (kind_id, k) in self._records}

    async def similar(
        self, text: str, kind_ids: Sequence[str], *, threshold: float, limit: int
    ) -> list[MatchCandidate]:
        found = []
        for (kind_id, key), title in self._records.items():
            if kind_id not in kind_ids:
                continue
            score = difflib.SequenceMatcher(None, title.lower(), text.lower()).ratio()
            if title.lower() == text.lower():
                score = 1.0
            if score >= threshold:
                found.append(MatchCandidate(kind_id, key, title, score, "trigram"))
        return sorted(found, key=lambda c: -c.score)[:limit]


@pytest.fixture
def ontology(supply_schema: Schema) -> Ontology:
    return build_ontology(supply_schema, None)


@pytest.fixture
def resolver(ontology: Ontology, supply_schema: Schema) -> Resolver:
    lookup = Lookup(supply_schema)
    dictionary = Dictionary(
        [TitleEntry(k, key, t) for (k, key), t in lookup._records.items()], min_chars=4
    )
    return Resolver(ontology, SETTINGS, lookup, dictionary)


def kind(schema: Schema, name: str) -> str:
    found = schema.kind_by_api_name(name)
    assert found is not None
    return found.id


def mention(kind_id: str, text: str, source: str = "title", start: int = 0) -> Mention:
    return Mention(
        kind_id=kind_id,
        text=text,
        start=start,
        end=start + len(text),
        chunk=0,
        score=1.0,
        tier=0,
        source=source,  # type: ignore[arg-type]
        extractor="test",
        document_id="doc_1",
    )


async def resolve_text(resolver: Resolver, ontology: Ontology, text: str) -> list[Resolved]:
    dictionary = resolver._dictionary
    found = tier0_mentions(
        ontology, dictionary, text, offset=0, chunk=0, document_id="doc_1", settings=SETTINGS
    )
    resolved = [await resolver.resolve(m) for m in non_overlapping(found)]
    return disambiguate(resolved, sentence_bounds(text))


def test_key_patterns_are_searchable() -> None:
    pattern = key_regex("^S-[0-9]{4,}$")
    assert pattern is not None
    assert [m.group() for m in pattern.finditer("S-1025, XS-1026 and S-12 or (S-10255)")] == [
        "S-1025",
        "S-10255",
    ]
    assert key_regex("^.*$") is None


async def test_keys_and_titles_resolve(
    resolver: Resolver, ontology: Ontology, supply_schema: Schema
) -> None:
    supplier, contract, part = (kind(supply_schema, n) for n in ("Supplier", "Contract", "Part"))
    existing = await resolver.resolve(mention(supplier, "S-1025", "key"))
    assert (existing.resolution.status, existing.key) == ("matched", "S-1025")
    new = await resolver.resolve(mention(contract, "C-3025", "key"))
    assert (new.resolution.status, new.key) == ("new", "C-3025")
    title = await resolver.resolve(mention(supplier, "Kavvik Filtration A.S."))
    assert (title.resolution.status, title.resolution.method) == ("matched", "title")
    folded = await resolver.resolve(mention(supplier, "KAVVIK FILTRATION A S"))
    assert (folded.key, folded.resolution.method) == ("S-1025", "normalized_title")
    fuzzy = await resolver.resolve(mention(supplier, "Kavik Filtration AS"))
    assert (fuzzy.key, fuzzy.resolution.method) == ("S-1025", "trigram")
    assert fuzzy.resolution.score < SETTINGS.confidence.normalized_title
    twins = await resolver.resolve(mention(part, "Needle roller bearing NK25"))
    assert twins.resolution.status == "ambiguous"
    assert {c.key for c in twins.resolution.candidates} == {"P-10023", "P-10033"}
    unknown = await resolver.resolve(mention(supplier, "Nobody Industries"))
    assert unknown.resolution.status == "unresolved"
    assert not unknown.usable
    assert normalize_title("Wexrix  Filtration Sp. z o.o.") == "wexrix filtration sp z o o"


async def test_a_key_in_the_sentence_settles_an_ambiguous_title(
    resolver: Resolver, ontology: Ontology
) -> None:
    text = "Part P-10033 (Needle roller bearing NK25) is new.\n\nNeedle roller bearing NK25 too."
    resolved = await resolve_text(resolver, ontology, text)
    first, second = resolved[0], resolved[1]
    assert (first.key, first.resolution.status) == ("P-10033", "matched")
    assert second.resolution.status == "ambiguous"  # alone in its sentence
    assert len(resolved) == 2  # the title in the first sentence names P-10033 too


async def test_values_follow_their_cues(
    resolver: Resolver, ontology: Ontology, supply_schema: Schema
) -> None:
    contract = supply_schema.kind_by_api_name("Contract")
    assert contract is not None
    owner = await resolver.resolve(mention(contract.id, "C-3013", "key"))
    text = (
        "The agreement starts on 8 February 2024 and ends on 7 February 2026. "
        "Delivery is DDP and payment is due 45 days after invoice. "
        "The supplier status is terminated."
    )
    found = {}
    for bounds in sentence_bounds(text):
        for value in sentence_values(
            ontology, [owner], text, bounds, excluded=[], settings=SETTINGS
        ):
            found[contract.attributes[value.attribute_id].api_name] = value
    assert {k: v.value for k, v in found.items()} == {
        "start_date": "2024-02-08",
        "end_date": "2026-02-07",
        "incoterm": "DDP",
        "payment_terms_days": "45",
    }
    assert all(0 < v.confidence <= SETTINGS.confidence.value_max for v in found.values())
    assert "status" not in found  # "supplier status" is about a Supplier


def test_tier2_schema_covers_only_the_fragment(supply_schema: Schema, ontology: Ontology) -> None:
    supplier, part = kind(supply_schema, "Supplier"), kind(supply_schema, "Part")
    schema = fragment_schema(ontology, [supplier, part])
    variants = schema["properties"]["records"]["items"]["anyOf"]
    assert [v["properties"]["kind"]["const"] for v in variants] == ["Supplier", "Part"]
    supplier_values = variants[0]["properties"]["values"]["properties"]
    assert "risk_score" in supplier_values
    assert supplier_values["risk_score"]["properties"]["value"]["type"] == "integer"
    assert "vat_number" not in supplier_values  # confidential
    assert "supplier_count" not in variants[1]["properties"]["values"]["properties"]  # derived
    assert schema["properties"]["links"]["items"]["properties"]["relation"]["enum"] == [
        "supplied_by"
    ]
    lone = fragment_schema(ontology, [supplier])
    assert "links" not in lone["properties"]
    assert lone["required"] == ["records"]


def test_evidence_must_be_found_and_hold_the_value(supply_schema: Schema) -> None:
    text = "Ends on\n7 February   2026, with 1,596,000.00 EUR."
    assert locate("on 7 February 2026", text) is not None
    assert locate("on 8 February 2026", text) is None
    contract = supply_schema.kind_by_api_name("Contract")
    assert contract is not None
    attribute = contract.attribute_by_api_name
    end, value, currency = attribute("end_date"), attribute("annual_value"), attribute("currency")
    assert end is not None
    assert value is not None
    assert currency is not None
    assert grounded(end, "2026-02-07", text)
    assert not grounded(end, "2026-02-08", text)
    assert grounded(value, "1596000.00", text)
    assert grounded(currency, "EUR", text)
    assert not grounded(currency, "PLN", text)


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("2024-01-23", "2024-01-23T00:00:00.000000Z"),
        ("2024-01-23T00:00:00", "2024-01-23T00:00:00.000000Z"),
        ("2024-01-23T10:15", "2024-01-23T10:15:00.000000Z"),
        ("2024-01-23T23:30:00-05:00", "2024-01-24T04:30:00.000000Z"),
    ],
)
async def test_tier2_timestamps_without_offset_are_read_as_utc_and_grounded_by_day(
    resolver: Resolver, ontology: Ontology, supply_schema: Schema, raw: str, canonical: str
) -> None:
    """Answers recorded from a hosted model: a document states an approval day, the model
    answers a timestamp without offset."""
    contract = kind(supply_schema, "Contract")
    text = "Contract C-3013 was approved on 23 January 2024 by contract-manager-1."
    chunk = Chunk(0, 0, len(text), text)
    hints = [await resolver.resolve(mention(contract, "C-3013", source="key"))]
    messages = build_messages(ontology, [contract], chunk, hints)
    answer: dict[str, Any] = {
        "records": [
            {
                "kind": "Contract",
                "ref": "C-3013",
                "evidence": "Contract C-3013",
                "values": {
                    "approved_at": {"value": raw, "evidence": "approved on 23 January 2024"},
                },
            }
        ]
    }
    settings = InferenceSettings.model_validate({"llm": {"provider": "fake"}})
    chat = FakeChatModel(settings.llm, settings.fake, {prompt_hash(messages): answer})
    result = await run_tier2(
        chat,
        ontology,
        chunk,
        hints,
        resolver,
        document_id="doc_1",
        schema_hash=b"\x00" * 32,
        model_key="fake:test",
        settings=SETTINGS,
        cache=None,
    )
    assert result.discarded == {}, result.discarded
    assert [v.value for v in result.values] == [canonical]
    assert result.discarded == {}


async def test_tier2_answers_are_verified(
    resolver: Resolver, ontology: Ontology, supply_schema: Schema
) -> None:
    supplier = kind(supply_schema, "Supplier")
    text = "Kavvik Filtration A.S. has a risk score of 81 since the review."
    chunk = Chunk(0, 0, len(text), text)
    hints = [await resolver.resolve(mention(supplier, "Kavvik Filtration A.S."))]
    messages = build_messages(ontology, [supplier], chunk, hints)
    answer: dict[str, Any] = {
        "records": [
            {
                "kind": "Supplier",
                "ref": "S-1025",
                "evidence": "Kavvik Filtration A.S.",
                "values": {
                    "risk_score": {"value": 81, "evidence": "risk score of 81"},
                    "city": {"value": "Izmir", "evidence": "risk score of 81"},
                    "country": {"value": "TR", "evidence": "in Turkey"},
                },
            },
            {"kind": "Supplier", "ref": "", "evidence": "Nobody", "values": {}},
        ]
    }
    settings = InferenceSettings.model_validate({"llm": {"provider": "fake"}})
    chat = FakeChatModel(settings.llm, settings.fake, {prompt_hash(messages): answer})
    result = await run_tier2(
        chat,
        ontology,
        chunk,
        hints,
        resolver,
        document_id="doc_1",
        schema_hash=b"\x00" * 32,
        model_key="fake:test",
        settings=SETTINGS,
        cache=None,
    )
    assert [(v.subject.key, v.value) for v in result.values] == [("S-1025", "81")]
    assert result.values[0].evidence.quote == "risk score of 81"
    assert result.values[0].tier == 2
    assert result.discarded == {"ungrounded_value": 1, "unverified_evidence": 2}
    assert result.usage.input_tokens > 0


async def test_candidates_become_items(
    resolver: Resolver, ontology: Ontology, supply_schema: Schema
) -> None:
    text = (
        "# Framework Supply Agreement C-3025\n\n"
        "The agreement starts on 8 February 2024.\n\n"
        "| Kavvik Filtration A.S. | S-1025 | P-10012 |\n"
        "| Wexrix Filtration Sp. z o.o. | S-1013 | C-3013 |"
    )
    resolved = await resolve_text(resolver, ontology, text)
    sentences = sentence_bounds(text)
    values = [
        v
        for bounds in sentences
        for v in sentence_values(
            ontology,
            [r for r in resolved if r.key == "C-3025"],
            text,
            bounds,
            excluded=[],
            settings=SETTINGS,
        )
    ]
    links = [
        *cooccurrence_links(
            ontology, resolved, text, sentences, document_id="d", settings=SETTINGS
        ),
        *heading_links(ontology, resolved, text, sentences, document_id="d", settings=SETTINGS),
    ]
    with_supplier = supply_schema.relation_by_api_name("with_supplier")
    supplied_by = supply_schema.relation_by_api_name("supplied_by")
    assert with_supplier is not None
    assert supplied_by is not None
    current = CurrentState(
        records={(kind(supply_schema, "Supplier"), "S-1013"): {}},
        links=frozenset({(with_supplier[0].id, "C-3013", "S-1013")}),
    )
    assembled = assemble_items(
        ontology,
        resolved,
        values,
        links,
        current,
        job_id="exj_1",
        principal="svc_scribe",
        now=NOW,
        settings=ExtractionSettings(auto_accept_threshold=0.6),
    )
    items = assembled.items
    assert [i.seq for i in items] == list(range(len(items)))
    assert [i.item_type for i in items] == ["record", "link", "link", "link"]
    record = items[0]
    assert record.subject["key"] == "C-3025"
    assert record.decision is ItemDecision.PENDING
    assert record.match is not None
    assert "title" in record.match["missing"]
    by_pair = {(i.subject["from"], i.subject["to"]): i for i in items[1:]}
    # The heading names C-3025: every Supplier of its section is a candidate party.
    assert set(by_pair) == {("P-10012", "S-1025"), ("C-3025", "S-1025"), ("C-3025", "S-1013")}
    assert by_pair[("P-10012", "S-1025")].decision is ItemDecision.ACCEPTED
    assert by_pair[("P-10012", "S-1025")].decided_by == "svc_scribe"
    assert by_pair[("C-3025", "S-1025")].decision is ItemDecision.PENDING  # new endpoint
    assert assembled.stats["unchanged"] == 1  # C-3013 with_supplier S-1013 exists

    summary = summarize(items, supply_schema)
    assert summary["items"] == 4
    assert summary["decisions"] == {"pending": 3, "accepted": 1, "rejected": 0}
    assert summary["types"] == {"link": 3, "record": 1}
    assert summary["targets"] == {"Contract": 1, "supplied_by": 1, "with_supplier": 2}
    title, description = proposal_text(summary, ["review.md"])
    assert title == "Extract review.md: 4 item(s), 1 accepted, 3 pending"
    assert "Contract 1" in description
