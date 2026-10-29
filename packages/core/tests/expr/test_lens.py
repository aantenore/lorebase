"""Lenses (design 5.13, 8.8): schema element, YAML form, validation and projection."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from lorebase_core.expr.visibility import element_ids, visible_schema
from lorebase_core.model import Schema, SchemaYamlError, dump_schema, load_schema
from lorebase_core.model.ids import IdPrefix, id_prefix
from lorebase_core.validate import IssueCode, SchemaRules, validate_schema

from .support import SUPPLY_SCHEMA

RULES = SchemaRules(allow_expressions=True)


def _document(**lenses: Any) -> dict[str, Any]:
    document = yaml.safe_load(SUPPLY_SCHEMA.read_text(encoding="utf-8"))
    document["lenses"] = lenses
    return document


def _schema(**lenses: Any) -> Schema:
    return load_schema(yaml.safe_dump(_document(**lenses)))


SUPPLIER_RISK = {
    "description": "A supplier with its parts and open risk flags.",
    "kinds": ["Supplier", "Part", "RiskFlag"],
    "traits": ["Located"],
    "relations": ["supplied_by", "flags"],
    "depth": 1,
    "include_recent_changes": False,
}


def test_a_lens_gets_an_id_and_names_resolve_to_ids() -> None:
    schema = _schema(supplier_risk=SUPPLIER_RISK)
    lens = schema.lens_by_api_name("supplier_risk")
    assert lens is not None
    assert id_prefix(lens.id) is IdPrefix.LENS
    names = {k.api_name for k in schema.kinds.values() if k.id in lens.kinds}
    assert names == {"Supplier", "Part", "RiskFlag"}
    assert [schema.traits[t].api_name for t in lens.traits] == ["Located"]
    assert {schema.relations[r].api_name for r in lens.relations} == {"supplied_by", "flags"}
    assert lens.depth == 1
    assert lens.include_recent_changes is False
    assert lens.include_schema is None
    assert lens.id in {e.id for e in schema.elements()}


def test_lenses_round_trip_through_yaml() -> None:
    schema = _schema(supplier_risk=SUPPLIER_RISK, everything={"description": "No limits."})
    text = dump_schema(schema)
    assert load_schema(text) == schema
    assert dump_schema(load_schema(text)) == text
    written = yaml.safe_load(text)["lenses"]["supplier_risk"]
    assert written["kinds"] == ["Supplier", "Part", "RiskFlag"]
    assert written["relations"] == ["supplied_by", "flags"]


def test_a_rename_keeps_the_lens_id() -> None:
    first = _schema(supplier_risk=SUPPLIER_RISK)
    lens = first.lens_by_api_name("supplier_risk")
    assert lens is not None
    document = _document(supplier_risk=SUPPLIER_RISK)
    document["lenses"]["supplier_risk"]["id"] = lens.id
    document["kinds"]["Supplier"]["id"] = next(
        k.id for k in first.kinds.values() if k.api_name == "Supplier"
    )
    document["lenses"]["risk"] = document["lenses"].pop("supplier_risk")
    renamed = load_schema(yaml.safe_dump(document))
    assert renamed.lens_by_api_name("risk") is not None
    assert next(iter(renamed.lenses)) == lens.id


@pytest.mark.parametrize(
    ("member", "value"),
    [("kinds", ["Nope"]), ("traits", ["Nope"]), ("relations", ["nope"])],
)
def test_a_lens_naming_an_unknown_element_does_not_load(member: str, value: list[str]) -> None:
    with pytest.raises(SchemaYamlError) as failure:
        _schema(broken={"description": "x", member: value})
    (problem,) = failure.value.problems
    assert problem.path[:3] == ("lenses", "broken", member)
    assert "unknown" in problem.message


def test_validation_refuses_dangling_references_and_ranges() -> None:
    schema = _schema(supplier_risk=SUPPLIER_RISK)
    assert validate_schema(schema, RULES) == []
    (lens_id,) = schema.lenses
    kind_id = schema.lenses[lens_id].kinds[0]
    broken = schema.model_copy(
        update={"kinds": {k: v for k, v in schema.kinds.items() if k != kind_id}}
    )
    assert IssueCode.SCHEMA_LENS_REFERENCE_UNKNOWN in {
        i.code for i in validate_schema(broken, RULES)
    }
    negative = schema.model_copy(
        update={"lenses": {lens_id: schema.lenses[lens_id].model_copy(update={"depth": -1})}}
    )
    (issue,) = [
        i for i in validate_schema(negative, RULES) if i.code is IssueCode.SCHEMA_LENS_INVALID
    ]
    assert issue.path == ("lenses", lens_id, "depth")
    empty_budget = schema.model_copy(
        update={"lenses": {lens_id: schema.lenses[lens_id].model_copy(update={"budget_tokens": 0})}}
    )
    assert IssueCode.SCHEMA_LENS_INVALID in {i.code for i in validate_schema(empty_budget, RULES)}


def test_lens_names_are_snake_case_and_unique() -> None:
    schema = _schema(Bad_Name={"description": "x"})
    assert any(i.code is IssueCode.SCHEMA_API_NAME_INVALID for i in validate_schema(schema, RULES))


def test_a_reader_of_some_kinds_sees_the_lens_cut_to_them() -> None:
    schema = _schema(supplier_risk=SUPPLIER_RISK)
    seen = visible_schema(schema, {"Supplier", "Part"})
    lens = seen.lens_by_api_name("supplier_risk")
    assert lens is not None
    assert {seen.kinds[k].api_name for k in lens.kinds} == {"Supplier", "Part"}
    assert {seen.relations[r].api_name for r in lens.relations} == {"supplied_by"}
    assert lens.depth == 1
    assert element_ids(seen) <= element_ids(schema)


def test_a_lens_with_no_readable_kind_does_not_exist_for_the_reader() -> None:
    schema = _schema(
        plants={"description": "Plants.", "kinds": ["Plant"]},
        trait_only={"description": "By Trait.", "traits": ["Located"]},
        open={"description": "No limits."},
    )
    seen = visible_schema(schema, {"Supplier", "Part"})
    assert seen.lens_by_api_name("plants") is None
    # Supplier implements Located: the Trait is still there and so is the lens.
    assert seen.lens_by_api_name("trait_only") is not None
    assert seen.lens_by_api_name("open") is not None
    plants_only = visible_schema(schema, {"Part"})
    assert plants_only.lens_by_api_name("trait_only") is None
    assert plants_only.lens_by_api_name("open") is not None
