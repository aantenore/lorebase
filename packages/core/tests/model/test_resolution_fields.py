"""Aliases and the value index flag of Kinds and Attributes (design 8.9)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lorebase_core.model import Attribute, IdPrefix, Kind, Schema, new_id, validate_schema
from lorebase_core.model.attribute import MAX_ALIAS_CHARS, MAX_ALIASES
from lorebase_core.model.yaml_schema import dump_schema, load_schema
from lorebase_core.types import TypeSpec
from lorebase_core.validate import IssueCode

KIND_ID = new_id(IdPrefix.KIND)
ATTRIBUTE_ID = new_id(IdPrefix.ATTRIBUTE)

SCHEMA = """
format: lorebase.schema/v1
domain: {id: shop, display_name: Shop}
kinds:
  Product:
    description: Something sold.
    aliases: [item, article]
    title_attribute: name
    attributes:
      name: {type: string, description: Name., required: true}
      color:
        type: string
        description: Main color.
        aliases: [colour]
        value_aliases: {Rosso: [red, rouge]}
        resolvable: true
      size:
        type: {enum: [S, M, L]}
        description: Size.
        value_aliases: {S: [small]}
      notes: {type: text, description: Free text., resolvable: false}
"""


def _schema() -> Schema:
    return load_schema(SCHEMA)


def test_yaml_round_trip_keeps_aliases_and_flags() -> None:
    schema = _schema()
    product = schema.kind_by_api_name("Product")
    assert product is not None
    assert product.aliases == ("item", "article")
    color = product.attribute_by_api_name("color")
    assert color is not None
    assert color.aliases == ("colour",)
    assert color.value_aliases == {"Rosso": ("red", "rouge")}
    assert color.resolvable is True
    again = load_schema(dump_schema(schema))
    assert again == schema


def test_unset_fields_stay_out_of_content() -> None:
    attribute = Attribute(id=ATTRIBUTE_ID, api_name="x", type=TypeSpec(name="string"))
    content = attribute.content()
    assert "aliases" not in content
    assert "value_aliases" not in content
    assert "resolvable" not in content
    blank = attribute.model_copy(update={})
    assert Attribute.model_validate({**content, "aliases": [" ", ""]}) == blank
    assert Attribute.model_validate({**content, "value_aliases": {"a": []}}) == blank


def test_aliases_are_cleaned_and_bounded() -> None:
    kind = Kind.model_validate(
        {"id": KIND_ID, "api_name": "Thing", "aliases": [" stuff ", "stuff", "", "object"]}
    )
    assert kind.aliases == ("stuff", "object")
    with pytest.raises(ValidationError):
        Kind.model_validate(
            {"id": KIND_ID, "api_name": "Thing", "aliases": [f"a{i}" for i in range(51)]}
        )
    with pytest.raises(ValidationError):
        Attribute.model_validate(
            {
                "id": ATTRIBUTE_ID,
                "api_name": "x",
                "type": "string",
                "aliases": ["a" * (MAX_ALIAS_CHARS + 1)],
            }
        )
    assert MAX_ALIASES == 50


def test_valid_schema_has_no_resolution_issues() -> None:
    issues = [
        i for i in validate_schema(_schema()) if i.code is IssueCode.SCHEMA_ATTRIBUTE_FLAG_INVALID
    ]
    assert issues == []


@pytest.mark.parametrize(
    ("change", "path_end"),
    [
        ({"notes": {"resolvable": True}}, "resolvable"),
        ({"notes": {"value_aliases": {"x": ["y"]}}}, "value_aliases"),
        ({"size": {"value_aliases": {"XL": ["huge"]}}}, "XL"),
        ({"color": {"sensitivity": ["confidential"]}}, "resolvable"),
    ],
)
def test_invalid_resolution_settings_are_reported(
    change: dict[str, dict[str, object]], path_end: str
) -> None:
    schema = _schema()
    product = schema.kind_by_api_name("Product")
    assert product is not None
    attributes = dict(product.attributes)
    for name, update in change.items():
        attribute = product.attribute_by_api_name(name)
        assert attribute is not None
        attributes[attribute.id] = Attribute.model_validate({**attribute.content(), **update})
    changed = schema.model_copy(
        update={
            "kinds": {
                **schema.kinds,
                product.id: product.model_copy(update={"attributes": attributes}),
            }
        }
    )
    issues = [
        i for i in validate_schema(changed) if i.code is IssueCode.SCHEMA_ATTRIBUTE_FLAG_INVALID
    ]
    assert issues, change
    assert any(str(i.path[-1]) == path_end for i in issues)
