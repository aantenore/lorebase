"""Schema discovery: observations of Kinds, Attributes and Relations outside the schema."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from lorebase_core.infer.suggestions import (
    Observation,
    SuggestionOptions,
    SuggestionType,
    proposed_kind,
)
from lorebase_core.model import Schema
from lorebase_inference.interfaces import ChatOptions, ChatResult, Message, Span, Usage
from lorebase_scribe.extraction.candidates import MatchCandidate, Resolved
from lorebase_scribe.extraction.discovery import Discovery, sample_value
from lorebase_scribe.extraction.matching import (
    Dictionary,
    Resolver,
    disambiguate,
    non_overlapping,
    tier0_mentions,
)
from lorebase_scribe.extraction.ontology import Ontology, build_ontology, fragment_schema
from lorebase_scribe.extraction.settings import DiscoverySettings, ExtractionSettings
from lorebase_scribe.extraction.text import Chunk, chunk_text, sentence_bounds
from lorebase_scribe.extraction.tier2 import cache_key, run_tier2

from .conftest import EXAMPLE

SETTINGS = ExtractionSettings()
OPTIONS = SuggestionOptions()
DOCUMENTS = EXAMPLE / "documents"


class NoRecords:
    """`RecordLookup` of an empty Domain: keys resolve as new Records."""

    async def titles_of_keys(self, kind_id: str, keys: Sequence[str]) -> dict[str, str | None]:
        return {}

    async def similar(
        self, text: str, kind_ids: Sequence[str], *, threshold: float, limit: int
    ) -> list[MatchCandidate]:
        return []


@pytest.fixture
def ontology(supply_schema: Schema) -> Ontology:
    return build_ontology(supply_schema, None)


def kind_id(schema: Schema, name: str) -> str:
    found = schema.kind_by_api_name(name)
    assert found is not None
    return found.id


async def observe(
    ontology: Ontology, text: str, settings: DiscoverySettings | None = None
) -> tuple[Discovery, list[Chunk], list[Resolved]]:
    resolver = Resolver(ontology, SETTINGS, NoRecords(), Dictionary([], min_chars=4))
    chunks = chunk_text(text, max_chars=2000, overlap_chars=200)
    mentions = [
        m
        for c in chunks
        for m in tier0_mentions(
            ontology,
            resolver._dictionary,
            c.text,
            offset=c.start,
            chunk=c.seq,
            document_id="doc_1",
            settings=SETTINGS,
        )
    ]
    resolved = disambiguate(
        [await resolver.resolve(m) for m in non_overlapping(mentions)], sentence_bounds(text)
    )
    discovery = Discovery(ontology, settings or DiscoverySettings(), OPTIONS)
    discovery.rules(text, chunks, resolved, document_id="doc_1")
    return discovery, chunks, resolved


def by_name(observations: Sequence[Observation]) -> dict[tuple[str, str], Observation]:
    return {(o.type.value, o.name): o for o in observations}


def read(name: str) -> str:
    return Path(DOCUMENTS / name).read_text()


async def test_a_heading_code_proposes_a_kind_that_owns_its_fields(
    ontology: Ontology, supply_schema: Schema
) -> None:
    text = read("quality-notice-p-10016.md")
    discovery, _, _ = await observe(ontology, text)
    found = by_name(discovery.finish({}))
    notice = found[("kind", "QualityNotice")]
    assert notice.value == "QN-2025-114"
    assert notice.evidence is not None
    assert notice.evidence["quote"] == "Quality notice QN-2025-114"
    assert text[notice.evidence["start"] : notice.evidence["end"]] == notice.evidence["quote"]
    owner = proposed_kind("Quality notice")
    # Fields naming a Kind, or whose value names a Record of one, are Relations.
    assert found[("relation", "part")].kind == owner
    assert found[("relation", "part")].target == kind_id(supply_schema, "Part")
    assert found[("relation", "supplier")].target == kind_id(supply_schema, "Supplier")
    assert found[("relation", "received_at")].target == kind_id(supply_schema, "Plant")
    assert found[("relation", "purchase_order")].target == kind_id(supply_schema, "PurchaseOrder")


async def test_field_lines_and_table_columns_outside_the_schema(
    ontology: Ontology, supply_schema: Schema
) -> None:
    contract, _, _ = await observe(ontology, read("contract-c-3025-kavvik-filtration.md"))
    found = by_name(contract.finish({}))
    buyer = found[("attribute", "buyer")]
    assert buyer.kind == kind_id(supply_schema, "Contract")
    assert buyer.value == "Corvane Industries, purchasing entity of the Distribution division"
    assert ("relation", "supplier") not in found  # Contract.with_supplier has it
    assert contract.stats["suggestions_known"] >= 1

    review, _, _ = await observe(ontology, read("supplier-review-2025-q4.md"))
    observations = review.finish({})
    supplier = kind_id(supply_schema, "Supplier")
    ends = [o for o in observations if o.type is SuggestionType.ATTRIBUTE and o.name == "ends"]
    assert {o.value for o in ends} == {"2026-02-07", "2026-02-09", "2026-04-12"}
    assert {o.kind for o in ends} == {supplier}
    sole = [o for o in observations if o.name == "sole_source_parts"]
    assert len(sole) == 3
    assert {(o.type, o.kind, o.target) for o in sole} == {
        (SuggestionType.RELATION, supplier, kind_id(supply_schema, "Part"))
    }
    names = {o.name for o in observations}
    assert "risk_score" not in names  # an Attribute of Supplier
    assert "agreement" not in names  # a synonym of Contract, linked by with_supplier
    assert "code" not in names  # the column of the row's own key


async def test_sensitive_attributes_and_values_are_withheld(
    ontology: Ontology, supply_schema: Schema
) -> None:
    text = (
        "# Supplier S-1025\n\n"
        "**VAT number:** TR1234567890\n"
        "**Bank contact:** Ayla Demir, TR1234567890\n"
        "**Audit score:** 87\n"
    )
    discovery, _, _ = await observe(ontology, text)
    supplier = kind_id(supply_schema, "Supplier")
    vat = next(
        a.id
        for a in supply_schema.kinds[supplier].attributes.values()
        if a.api_name == "vat_number"
    )
    observations = discovery.finish({(supplier, "S-1025"): {vat: "TR1234567890"}})
    found = by_name(observations)
    assert ("attribute", "vat_number") not in found
    assert discovery.stats["suggestions_withheld_sensitive"] == 1
    contact = found[("attribute", "bank_contact")]
    assert contact.evidence is None
    assert contact.value is None
    assert discovery.stats["suggestion_evidence_withheld"] == 1
    assert found[("attribute", "audit_score")].value == "87"


async def test_rules_can_be_turned_off(ontology: Ontology) -> None:
    discovery, _, _ = await observe(
        ontology, read("quality-notice-p-10016.md"), DiscoverySettings(rules=False)
    )
    assert discovery.finish({}) == []


def test_sample_values_are_normalized() -> None:
    assert sample_value("**7 Feb 2026**", 120) == "2026-02-07"
    assert sample_value("1,596,000.00", 120) == "1596000.00"
    assert sample_value("Poznan Warehouse (PL-11).", 10) == "Poznan War"
    assert sample_value("  ", 120) is None


async def test_discovery_span_labels_propose_kinds(ontology: Ontology) -> None:
    discovery = Discovery(
        ontology, DiscoverySettings(span_labels=("certificate", "Supplier")), OPTIONS
    )
    assert discovery.span_labels() == ["certificate"]
    chunk = Chunk(0, 100, 130, "Holds the ISO 9001 certificate.")
    discovery.spans(
        [Span("certificate", "ISO 9001", 10, 18, 0.8), Span("Supplier", "x", 0, 1)],
        chunk,
        document_id="doc_1",
        model="fake",
    )
    (observation,) = discovery.finish({})
    assert (observation.type, observation.name, observation.value) == (
        SuggestionType.KIND,
        "Certificate",
        "ISO 9001",
    )
    assert observation.evidence is not None
    assert (observation.evidence["start"], observation.evidence["end"]) == (110, 118)


class Answer:
    """`ChatModel` returning one canned answer and recording the schema it was given."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.schemas: list[dict[str, Any]] = []

    async def complete_json(
        self,
        messages: Sequence[Message],
        schema: dict[str, Any],
        options: ChatOptions | None = None,
    ) -> ChatResult:
        self.schemas.append(schema)
        return ChatResult(data=self.data, usage=Usage(10, 10), model="fake:test")


async def test_tier2_reports_facts_outside_the_schema(
    ontology: Ontology, supply_schema: Schema
) -> None:
    text = (
        "Kavvik Filtration A.S. (S-1025) holds the ISO 9001 certificate. Its quality manager "
        "is reachable at extension 4410. S-1025 is audited by S-1013."
    )
    _, chunks, resolved = await observe(ontology, text)
    answer = Answer(
        {
            "records": [
                {
                    "kind": "Supplier",
                    "ref": "S-1025",
                    "evidence": "Kavvik Filtration A.S. (S-1025)",
                    "values": {
                        "supplier_code": {"value": "S-1025", "evidence": "(S-1025)"},
                        "phone_extension": {"value": "4410", "evidence": "extension 4410"},
                        "made_up": {"value": "nothing", "evidence": "not in the text"},
                    },
                },
                {"kind": "Certificate", "ref": "ISO 9001", "evidence": "ISO 9001 certificate"},
            ],
            "unlisted": [
                {
                    "type": "relation",
                    "name": "audited by",
                    "subject": "S-1025",
                    "value": "S-1013",
                    "evidence": "S-1025 is audited by S-1013",
                },
                {
                    "type": "attribute",
                    "name": "quality manager",
                    "subject": "S-1025",
                    "value": "Somebody",
                    "evidence": "Its quality manager",
                },
            ],
        }
    )
    resolver = Resolver(ontology, SETTINGS, NoRecords(), Dictionary([], min_chars=4))
    result = await run_tier2(
        answer,
        ontology,
        chunks[0],
        resolved,
        resolver,
        document_id="doc_1",
        schema_hash=b"\0" * 32,
        model_key="fake:test",
        settings=SETTINGS,
        cache=None,
        discover=True,
    )
    assert "unlisted" in answer.schemas[0]["properties"]
    found = {(o.type.value, o.name): o for o in result.observations}
    supplier = kind_id(supply_schema, "Supplier")
    assert found[("attribute", "phone_extension")].kind == supplier
    assert found[("attribute", "phone_extension")].value == "4410"
    assert found[("kind", "Certificate")].value == "ISO 9001"
    audited = found[("relation", "audited_by")]
    assert (audited.kind, audited.target) == (supplier, supplier)
    assert ("attribute", "made_up") not in found  # evidence not in the chunk
    assert ("attribute", "quality_manager") not in found  # value not in its quote
    assert result.discarded["ungrounded_value"] == 1


async def test_tier2_without_discovery_keeps_its_schema_and_cache_key(
    ontology: Ontology,
) -> None:
    kinds = list(ontology.kinds)
    assert "unlisted" not in fragment_schema(ontology, kinds)["properties"]
    assert "unlisted" in fragment_schema(ontology, kinds, unlisted=True)["properties"]
    chunk = Chunk(0, 0, 4, "text")
    plain = cache_key("m", "v", b"\0" * 32, chunk, kinds)
    assert plain != cache_key("m", "v", b"\0" * 32, chunk, kinds, discover=True)
    assert plain == cache_key("m", "v", b"\0" * 32, chunk, list(reversed(kinds)))
