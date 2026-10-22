"""Schema suggestions: names, matching against a schema, types from samples, change set patch."""

from __future__ import annotations

import pytest

from lorebase_core.infer.suggestions import (
    Observation,
    Suggestion,
    SuggestionError,
    SuggestionOptions,
    SuggestionType,
    existing_match,
    field_name,
    fingerprint,
    infer_attribute,
    kind_name,
    name_similarity,
    proposed_kind,
    suggestion_patch,
)
from lorebase_core.model import Schema, load_schema, schema_from_document, schema_to_document
from lorebase_core.model.attribute import Sensitivity
from lorebase_core.types import AttributeType
from lorebase_core.validate import SchemaRules
from lorebase_core.validate.schema_rules import validate_schema

from .support import SUPPLY_DIR

OPTIONS = SuggestionOptions()


@pytest.fixture(scope="module")
def schema() -> Schema:
    return load_schema((SUPPLY_DIR / "schema.yaml").read_text())


def kind(schema: Schema, name: str) -> str:
    found = schema.kind_by_api_name(name)
    assert found is not None
    return found.id


def test_names_and_fingerprints() -> None:
    assert kind_name("quality notices") == "QualityNotice"
    assert field_name("Received at") == "received_at"
    assert proposed_kind("Quality notice") == "+QualityNotice"
    seen = Observation(SuggestionType.ATTRIBUTE, "Lead Times", "doc", 0.5, "rules", kind="k")
    assert seen.name == "lead_times"
    assert seen.fingerprint == fingerprint(SuggestionType.ATTRIBUTE, "lead_time", "k")
    assert fingerprint(SuggestionType.KIND, "Certificates") == "kind:certificate"


def test_similarity_uses_singulars_synonyms_and_ratio() -> None:
    assert name_similarity("Suppliers", "supplier", OPTIONS) == 1.0
    assert name_similarity("vendor", "Supplier", OPTIONS) == 1.0
    assert name_similarity("lead_time", "lead_times_days", OPTIONS) < OPTIONS.name_similarity
    assert name_similarity("risk score", "risk_scores", OPTIONS) == 1.0
    assert name_similarity("", "x", OPTIONS) == 0.0


def test_existing_elements_are_matched(schema: Schema) -> None:
    supplier, contract = kind(schema, "Supplier"), kind(schema, "Contract")
    kind_match = existing_match(schema, SuggestionType.KIND, "Vendors", options=OPTIONS)
    assert kind_match is not None
    assert kind_match.api_name == "Supplier"
    assert existing_match(schema, SuggestionType.KIND, "Quality notice", options=OPTIONS) is None
    code = existing_match(schema, SuggestionType.ATTRIBUTE, "Code", kind=supplier, options=OPTIONS)
    assert code is not None
    assert code.api_name == "supplier_code"
    vat = existing_match(
        schema, SuggestionType.ATTRIBUTE, "VAT number", kind=supplier, options=OPTIONS
    )
    assert vat is not None
    assert vat.sensitive
    assert Sensitivity.CONFIDENTIAL in vat.tags
    named = existing_match(
        schema, SuggestionType.ATTRIBUTE, "Supplier", kind=contract, options=OPTIONS
    )
    assert named is not None
    assert named.section == "kinds"
    assert (
        existing_match(schema, SuggestionType.ATTRIBUTE, "Buyer", kind=contract, options=OPTIONS)
        is None
    )
    relation = existing_match(
        schema,
        SuggestionType.RELATION,
        "agreement",
        kind=supplier,
        target=contract,
        options=OPTIONS,
    )
    assert relation is not None
    assert relation.api_name == "with_supplier"
    # A proposed Kind that exists by now resolves to it.
    owned = existing_match(
        schema, SuggestionType.ATTRIBUTE, "legal name", kind="+Vendor", options=OPTIONS
    )
    assert owned is not None
    assert owned.api_name == "legal_name"
    assert (
        existing_match(schema, SuggestionType.ATTRIBUTE, "x", kind="+Unknown", options=OPTIONS)
        is None
    )


def test_types_come_from_sample_values() -> None:
    assert infer_attribute("ends", ["2026-02-07", "2026-04-12"], OPTIONS).type.name is (
        AttributeType.DATE
    )
    assert infer_attribute("score", ["87", "12", "40"], OPTIONS).type.name is AttributeType.INTEGER
    guess = infer_attribute("buyer", ["Corvane Industries"], OPTIONS)
    assert guess.type.name is AttributeType.STRING
    assert 0 < guess.confidence < 1


def test_patch_adds_valid_elements(schema: Schema) -> None:
    contract, part = kind(schema, "Contract"), kind(schema, "Part")
    suggestions = [
        Suggestion(
            "s1",
            SuggestionType.KIND,
            "QualityNotice",
            samples=("QN-2025-114", "QN-2025-120"),
            labels=("Quality notice",),
        ),
        Suggestion(
            "s2",
            SuggestionType.ATTRIBUTE,
            "received_on",
            kind="+QualityNotice",
            samples=("2025-10-27",),
        ),
        Suggestion("s3", SuggestionType.RELATION, "part", kind="+QualityNotice", target=part),
        Suggestion(
            "s4", SuggestionType.ATTRIBUTE, "buyer", kind=contract, samples=("Corvane Industries",)
        ),
    ]
    patch = suggestion_patch(schema, suggestions, OPTIONS)
    assert set(patch["kinds"]) == {"Contract", "QualityNotice"}
    assert set(patch["relations"]) == {"part"}
    notice = patch["kinds"]["QualityNotice"]
    assert notice["primary_key"] == ["quality_notice_code"]
    assert notice["attributes"]["quality_notice_code"]["constraints"]["pattern"] == (
        "^QN-[0-9]{4,}-[0-9]{3,}$"
    )
    assert notice["attributes"]["received_on"]["type"] == "date"
    assert patch["relations"]["part"]["from_kind"] == "QualityNotice"
    assert patch["relations"]["part"]["to_kind"] == "Part"
    # Existing Kinds keep every element and id; the result validates.
    assert set(schema.kinds[contract].attributes) < {
        a["id"] for a in patch["kinds"]["Contract"]["attributes"].values()
    }
    document = schema_to_document(schema)
    for section, elements in patch.items():
        document[section] = {**document[section], **elements}
    target = schema_from_document(document, base=schema)
    rules = SchemaRules(allow_expressions=True)
    assert [i for i in validate_schema(target, rules) if i.is_error] == []
    assert patch["kinds"]["QualityNotice"]["status"] == "draft"


def test_patch_refuses_unknown_kinds_and_duplicates(schema: Schema) -> None:
    with pytest.raises(SuggestionError) as error:
        suggestion_patch(
            schema,
            [Suggestion("s1", SuggestionType.ATTRIBUTE, "x", kind="+QualityNotice")],
            OPTIONS,
        )
    assert error.value.suggestion == "s1"
    with pytest.raises(SuggestionError):
        suggestion_patch(schema, [Suggestion("s2", SuggestionType.KIND, "Supplier")], OPTIONS)
    titled = suggestion_patch(schema, [Suggestion("s3", SuggestionType.KIND, "Warehouse")], OPTIONS)
    assert titled["kinds"]["Warehouse"]["title_attribute"] == "name"
    assert "primary_key" not in titled["kinds"]["Warehouse"]
