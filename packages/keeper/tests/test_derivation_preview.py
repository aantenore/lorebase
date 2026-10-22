"""Candidate Derivations without a database: change set, static checks, Kinds touched."""

from __future__ import annotations

from pathlib import Path

import pytest

from lorebase_core.model import Schema, load_schema
from lorebase_keeper.reading.settings import ReadSettings
from lorebase_keeper.services.derivation_preview import (
    DERIVATION_NOT_FOUND,
    DERIVATION_UNSUPPORTED,
    CandidateInvalidError,
    candidate_of,
    kind_names,
)
from lorebase_keeper.services.schema import ChangeSetInput
from lorebase_store.settings import SchemaSettings

SUPPLY_SCHEMA = Path(__file__).resolve().parents[3] / "examples" / "supply" / "schema.yaml"
RULES = SchemaSettings().rules()


@pytest.fixture
def supply() -> Schema:
    return load_schema(SUPPLY_SCHEMA.read_text())


def edit(name: str, **definition: object) -> ChangeSetInput:
    """A patch replacing fields of the definition of a stored Derivation."""
    return ChangeSetInput(patch={"derivations": {name: {"definition": definition}}})


def test_empty_patch_is_the_derivation_as_saved(supply: Schema) -> None:
    candidate = candidate_of(supply, ChangeSetInput(patch={}), "single_sourced", RULES)
    assert candidate.kind.api_name == "Part"
    assert candidate.attribute.api_name == "single_sourced"
    assert candidate.derivation.id == supply.derivation_by_api_name("single_sourced").id  # type: ignore[union-attr]


def test_edited_expression_is_the_candidate(supply: Schema) -> None:
    change = edit(
        "single_sourced",
        variant="expression",
        kind="Part",
        attribute="single_sourced",
        expression="supplier_count <= 1",
    )
    candidate = candidate_of(supply, change, "single_sourced", RULES)
    assert candidate.derivation.definition.expression == "supplier_count <= 1"  # type: ignore[union-attr]
    # The stored schema is untouched.
    stored = supply.derivation_by_api_name("single_sourced")
    assert stored.definition.expression == "supplier_count == 1"  # type: ignore[union-attr]


def test_expression_is_type_checked_with_the_schema_limits(supply: Schema) -> None:
    wrong = edit(
        "single_sourced",
        variant="expression",
        kind="Part",
        attribute="single_sourced",
        expression="supplier_count + 'x'",
    )
    with pytest.raises(CandidateInvalidError) as invalid:
        candidate_of(supply, wrong, "single_sourced", RULES)
    assert [i.code for i in invalid.value.issues] == ["schema.expression_invalid"]
    assert invalid.value.issues[0].path[:2] == ("derivations", "single_sourced")

    long = edit(
        "single_sourced",
        variant="expression",
        kind="Part",
        attribute="single_sourced",
        expression="supplier_count == 1" + " && true" * 10,
    )
    tight = SchemaSettings(expression_limits={"max_source_length": 20}).rules()
    with pytest.raises(CandidateInvalidError) as limited:
        candidate_of(supply, long, "single_sourced", tight)
    assert ("derivations", "single_sourced") in {
        tuple(i.path[:2]) for i in limited.value.issues if i.code == "schema.expression_invalid"
    }


def test_expressions_can_be_disabled(supply: Schema) -> None:
    disabled = SchemaSettings(expressions_enabled=False).rules()
    with pytest.raises(CandidateInvalidError) as invalid:
        candidate_of(supply, ChangeSetInput(patch={}), "single_sourced", disabled)
    assert "schema.expressions_disabled" in {i.code for i in invalid.value.issues}


def test_unknown_derivation_and_unreadable_documents(supply: Schema) -> None:
    with pytest.raises(CandidateInvalidError) as missing:
        candidate_of(supply, ChangeSetInput(patch={}), "nothing", RULES)
    assert [i.code for i in missing.value.issues] == [DERIVATION_NOT_FOUND]
    broken = ChangeSetInput(patch={"derivations": {"x": {"definition": {"variant": "nope"}}}})
    with pytest.raises(CandidateInvalidError) as unreadable:
        candidate_of(supply, broken, "x", RULES)
    assert unreadable.value.issues


def test_path_derivations_compute_no_values(supply: Schema) -> None:
    steps = [{"relation": "supplied_by"}, {"relation": "supplied_by", "direction": "inverse"}]
    change = ChangeSetInput(
        patch={
            "derivations": {
                "co_parts": {
                    "description": "Parts that share a supplier.",
                    "definition": {"variant": "path", "steps": steps},
                }
            }
        }
    )
    with pytest.raises(CandidateInvalidError) as unsupported:
        candidate_of(supply, change, "co_parts", RULES)
    assert [i.code for i in unsupported.value.issues] == [DERIVATION_UNSUPPORTED]


def test_touched_kinds_follow_relations_and_links(supply: Schema) -> None:
    aggregate = candidate_of(supply, ChangeSetInput(patch={}), "supplier_count", RULES)
    assert kind_names(aggregate.schema, aggregate.touched) == ["Part", "Supplier"]
    local = candidate_of(supply, ChangeSetInput(patch={}), "single_sourced", RULES)
    assert kind_names(local.schema, local.touched) == ["Part"]
    linked = edit(
        "single_sourced",
        variant="expression",
        kind="Part",
        attribute="single_sourced",
        expression="links.supplied_by.exists(s, s.risk_score > 50)",
    )
    through_links = candidate_of(supply, linked, "single_sourced", RULES)
    assert kind_names(through_links.schema, through_links.touched) == ["Part", "Supplier"]


def test_sample_size_is_a_setting() -> None:
    bounds = ReadSettings().derivation_preview
    assert (bounds.default_limit, bounds.max_limit) == (5, 25)
