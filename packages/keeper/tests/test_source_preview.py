"""Expression mappings of a candidate Source without a database."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from lorebase_core.expr import ExprLimits
from lorebase_core.model import Schema, load_schema
from lorebase_keeper.reading.settings import ReadSettings
from lorebase_keeper.services.derivation_preview import CandidateInvalidError
from lorebase_keeper.services.schema import ChangeSetInput
from lorebase_keeper.services.source_preview import (
    SOURCE_NOT_FOUND,
    candidate_of,
    evaluate,
    expression_attributes,
)
from lorebase_store.settings import SchemaSettings

SUPPLY_SCHEMA = Path(__file__).resolve().parents[3] / "examples" / "supply" / "schema.yaml"
RULES = SchemaSettings().rules()
NOW = datetime(2026, 10, 21, 9, 30, tzinfo=UTC)


@pytest.fixture
def supply() -> Schema:
    return load_schema(SUPPLY_SCHEMA.read_text())


def edited(**mapping: dict[str, Any]) -> ChangeSetInput:
    """A patch replacing attribute mappings of the stored Source."""
    document = yaml.safe_load(SUPPLY_SCHEMA.read_text())
    source = document["sources"]["approved.suppliers"]
    source["attribute_mapping"] = {**source["attribute_mapping"], **mapping}
    return ChangeSetInput(patch={"sources": {"approved.suppliers": source}})


def test_only_expression_mappings_are_evaluated(supply: Schema) -> None:
    change = edited(
        legal_name={"expression": 'name.trim() + " (" + country + ")"'},
        city={"expression": 'row["Town"]'},
    )
    candidate = candidate_of(supply, change, "approved.suppliers", RULES)
    assert expression_attributes(candidate) == {
        "legal_name": 'name.trim() + " (" + country + ")"',
        "city": 'row["Town"]',
    }
    rows = [
        {"name": " Kavsen ", "country": "PL", "Town": "Poznan"},
        {"name": "Orlen", "country": "PL", "Town": "Plock"},
    ]
    cells = evaluate(candidate, rows, limits=ExprLimits(), now=NOW)
    assert [{a: c.value for a, c in row.items()} for row in cells] == [
        {"legal_name": "Kavsen (PL)", "city": "Poznan"},
        {"legal_name": "Orlen (PL)", "city": "Plock"},
    ]
    assert all(c.error_code is None for row in cells for c in row.values())


def test_a_source_without_expressions_has_no_cells(supply: Schema) -> None:
    candidate = candidate_of(supply, ChangeSetInput(patch={}), "approved.suppliers", RULES)
    assert expression_attributes(candidate) == {}
    assert evaluate(candidate, [{"a": "b"}], limits=ExprLimits(), now=NOW) == [{}]


def test_errors_are_per_row_and_per_attribute(supply: Schema) -> None:
    change = edited(
        risk_score={"expression": "int(score) + 1"}, city={"expression": "unknown_column"}
    )
    candidate = candidate_of(supply, change, "approved.suppliers", RULES)
    first, second = evaluate(
        candidate, [{"score": "41"}, {"score": "n/a"}], limits=ExprLimits(), now=NOW
    )
    assert first["risk_score"].value == 42
    assert second["risk_score"].error_code == "expression.evaluation"
    assert first["city"].error_code == second["city"].error_code == "expression.type"
    assert "unknown_column" in (first["city"].error_message or "")


def test_the_limits_of_the_engine_apply(supply: Schema) -> None:
    candidate = candidate_of(
        supply, edited(city={"expression": "a + b + c"}), "approved.suppliers", RULES
    )
    cells = evaluate(
        candidate, [{"a": "x", "b": "y", "c": "z"}], limits=ExprLimits(max_source_length=3), now=NOW
    )
    assert cells[0]["city"].error_code == "expression.limit_exceeded"


def test_unknown_sources_and_unreadable_change_sets(supply: Schema) -> None:
    with pytest.raises(CandidateInvalidError) as missing:
        candidate_of(supply, ChangeSetInput(patch={}), "nothing", RULES)
    assert [i.code for i in missing.value.issues] == [SOURCE_NOT_FOUND]
    broken = ChangeSetInput(patch={"sources": {"x": {"type": "nope"}}})
    with pytest.raises(CandidateInvalidError) as unreadable:
        candidate_of(supply, broken, "x", RULES)
    assert unreadable.value.issues


def test_the_row_count_is_a_setting() -> None:
    assert ReadSettings().source_preview_max_rows == 100
