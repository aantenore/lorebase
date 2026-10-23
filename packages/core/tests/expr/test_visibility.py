"""The schema a reader limited to some Kinds sees: nothing tells about the other Kinds."""

from __future__ import annotations

import itertools
import json
from typing import Any

import pytest
import yaml

from lorebase_core.expr.visibility import element_ids, visible_schema
from lorebase_core.model import Schema, load_schema
from lorebase_core.validate import SchemaRules, validate_schema

from .support import SUPPLY_SCHEMA, supply

RULES = SchemaRules(allow_expressions=True)


def _variant(change: Any) -> Schema:
    """The supply schema with its YAML document changed by `change`."""
    document = yaml.safe_load(SUPPLY_SCHEMA.read_text(encoding="utf-8"))
    change(document)
    return load_schema(yaml.safe_dump(document))


def _names(schema: Schema, section: str) -> set[str]:
    return {element.api_name for element in getattr(schema, section).values()}


def _attributes(schema: Schema, kind: str) -> set[str]:
    found = schema.kind_by_api_name(kind)
    assert found is not None
    return {a.api_name for a in found.attributes.values()}


def test_every_kind_readable_is_the_schema_itself() -> None:
    schema = supply()
    assert visible_schema(schema, _names(schema, "kinds")) is schema


def test_other_kinds_and_what_tells_about_them_are_left_out() -> None:
    schema = supply()
    seen = visible_schema(schema, {"Part", "Plant"})
    assert _names(seen, "kinds") == {"Part", "Plant"}
    assert _names(seen, "relations") == {"stocked_at"}
    # supplier_count counts Suppliers; single_sourced reads supplier_count.
    assert _names(seen, "derivations") == set()
    assert _attributes(seen, "Part") == _attributes(schema, "Part") - {
        "supplier_count",
        "single_sourced",
    }
    # Plant implements Located: the Trait stays.
    assert _names(seen, "traits") == {"Located"}
    assert _names(seen, "operations") == set()
    assert _names(seen, "sources") == set()
    assert seen.link_by_api_name("supplied_by") is None
    assert seen.kind_by_api_name("Supplier") is None


def test_references_to_other_kinds_are_left_out() -> None:
    schema = supply()
    seen = visible_schema(schema, {"PurchaseOrder", "Part", "Plant"})
    assert "supplier" not in _attributes(seen, "PurchaseOrder")
    assert _names(seen, "relations") == {"stocked_at", "for_part", "delivered_to"}
    # The Source of purchase orders maps nothing left out but its target is kept only when
    # every Relation and Attribute it writes is visible.
    assert _names(seen, "sources") == set()


def test_operations_naming_other_kinds_are_left_out() -> None:
    schema = supply()
    seen = visible_schema(schema, {"Contract", "Part", "Plant", "PurchaseOrder"})
    assert _names(seen, "operations") == {"approve_contract"}


@pytest.mark.parametrize(
    "size", range(len(supply().kinds) + 1), ids=lambda size: f"{size}-kinds"
)
def test_no_projection_names_an_element_it_leaves_out(size: int) -> None:
    schema = supply()
    kinds = sorted(_names(schema, "kinds"))
    every = element_ids(schema)
    for chosen in itertools.combinations(kinds, size):
        seen = visible_schema(schema, set(chosen))
        assert _names(seen, "kinds") <= set(chosen)
        hidden = every - element_ids(seen)
        document = json.dumps(seen.model_dump(mode="json"))
        assert not [i for i in hidden if i in document], chosen
        assert not [i for i in validate_schema(seen, RULES) if i.is_error], chosen


def test_expressions_following_links_to_other_kinds_are_left_out() -> None:
    def change(document: dict[str, Any]) -> None:
        part = document["kinds"]["Part"]["attributes"]
        part["linked"] = {"type": "boolean", "id": "att_01KEZF7C00ZZZZZZZZZZZZZZZZ"}
        document["derivations"]["linked"] = {
            "id": "drv_01KEZF7C00ZZZZZZZZZZZZZZZZ",
            "definition": {
                "variant": "expression",
                "kind": "Part",
                "attribute": "linked",
                "expression": "size(links.supplied_by) > 0",
            },
        }

    schema = _variant(change)
    assert "linked" in _attributes(visible_schema(schema, {"Part", "Supplier"}), "Part")
    assert "linked" not in _attributes(visible_schema(schema, {"Part", "Plant"}), "Part")


def test_a_kind_indexed_or_titled_from_other_kinds_is_left_out() -> None:
    def searchable(document: dict[str, Any]) -> None:
        document["kinds"]["Part"]["attributes"]["supplier_count"]["searchable"] = True

    schema = _variant(searchable)
    assert _names(visible_schema(schema, {"Part", "Plant"}), "kinds") == {"Plant"}
    assert _names(visible_schema(schema, {"Part", "Supplier"}), "kinds") == {"Part", "Supplier"}


def test_a_kind_keyed_by_a_reference_to_another_kind_is_left_out() -> None:
    def keyed(document: dict[str, Any]) -> None:
        document["kinds"]["PurchaseOrder"]["primary_key"] = ["supplier", "po_number"]

    schema = _variant(keyed)
    assert _names(visible_schema(schema, {"PurchaseOrder", "Part"}), "kinds") == {"Part"}
    assert "PurchaseOrder" in _names(
        visible_schema(schema, {"PurchaseOrder", "Supplier"}), "kinds"
    )
