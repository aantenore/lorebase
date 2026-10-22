"""The Kinds each sensitivity tag of a derived value comes from."""

from __future__ import annotations

import yaml

from lorebase_core.expr.sensitivity import effective_origins, tag_origins
from lorebase_core.model import Schema, Sensitivity, load_schema

from .support import SUPPLY_SCHEMA


def _schema(filter_expression: str | None) -> Schema:
    """The supply schema with `supplier_count` counting Suppliers that match a filter."""
    document = yaml.safe_load(SUPPLY_SCHEMA.read_text(encoding="utf-8"))
    if filter_expression is not None:
        document["derivations"]["supplier_count"]["definition"]["filter"] = filter_expression
    return load_schema(yaml.safe_dump(document))


def _ids(schema: Schema, kind: str, attribute: str) -> tuple[str, str]:
    found = schema.kind_by_api_name(kind)
    assert found is not None
    target = found.attribute_by_api_name(attribute)
    assert target is not None
    return found.id, target.id


def test_tags_come_from_the_kinds_of_what_a_derivation_reads() -> None:
    schema = _schema('vat_number != ""')
    part, count = _ids(schema, "Part", "supplier_count")
    supplier, _ = _ids(schema, "Supplier", "vat_number")
    assert tag_origins(schema, count) == {Sensitivity.CONFIDENTIAL: frozenset({supplier})}
    # Transitively: single_sourced reads supplier_count.
    _, single = _ids(schema, "Part", "single_sourced")
    assert tag_origins(schema, single) == {Sensitivity.CONFIDENTIAL: frozenset({supplier})}
    # Tags from elsewhere (the default branch's) count on the Kind holding the Attribute.
    assert tag_origins(schema, single, {single: frozenset({Sensitivity.PII})}) == {
        Sensitivity.CONFIDENTIAL: frozenset({supplier}),
        Sensitivity.PII: frozenset({part}),
    }


def test_values_read_from_nothing_sensitive_have_no_origins() -> None:
    schema = _schema(None)
    _, count = _ids(schema, "Part", "supplier_count")
    assert tag_origins(schema, count) == {}
    assert tag_origins(schema, "att_unknown") == {}


def test_origins_of_every_attribute_at_once() -> None:
    schema = _schema('vat_number != ""')
    supplier, vat = _ids(schema, "Supplier", "vat_number")
    _, count = _ids(schema, "Part", "supplier_count")
    _, single = _ids(schema, "Part", "single_sourced")
    every = effective_origins(schema)
    assert every[vat] == {Sensitivity.CONFIDENTIAL: frozenset({supplier})}
    assert every[count] == every[single] == tag_origins(schema, count)
    assert all(tag_origins(schema, attribute) == found for attribute, found in every.items())
