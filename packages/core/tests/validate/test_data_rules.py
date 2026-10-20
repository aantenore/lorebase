from __future__ import annotations

import pytest

from lorebase_core.model import Link, Record, Schema, TraitAttribute
from lorebase_core.types import Constraints, TypeSpec
from lorebase_core.validate import IssueCode, validate_link, validate_record

from ..model.supply import (
    CONTRACT,
    CONTRACT_VALUE,
    IS_PRIMARY,
    LEGAL_NAME,
    LOCATED,
    LOCATED_COUNTRY,
    MADE_AT,
    RISK_SCORE,
    SUPPLIED_BY,
    SUPPLIER,
    SUPPLIER_CITY,
    SUPPLIER_CODE,
    SUPPLIER_COUNT,
    SUPPLIER_COUNTRY,
    TAGS,
    TIER,
    ident,
    replace_attribute,
    replace_element,
    supply_schema,
)
from .data import CONTRACT_1, MemoryIndex, contract, link, part, supplier

C = IssueCode


def codes_of(schema: Schema, record: Record) -> list[IssueCode]:
    return [issue.code for issue in validate_record(schema, record)]


RECORD_CASES: list[tuple[str, Record, list[IssueCode]]] = [
    ("valid-supplier", supplier("S-1", **{RISK_SCORE: "40", TIER: "2"}), []),
    ("valid-minted-key", contract(CONTRACT_1), []),
    ("valid-part-with-tags", part("P-1", **{TAGS: ["a", "b"]}), []),
    (
        "kind-unknown",
        Record(kind=ident("knd", 99), key="x"),
        [C.RECORD_KIND_UNKNOWN],
    ),
    (
        "key-mismatch",
        Record(kind=SUPPLIER, key="S-2", values=supplier("S-1").values),
        [C.RECORD_KEY_MISMATCH],
    ),
    ("minted-key-invalid", contract("C-1"), [C.RECORD_KEY_INVALID]),
    ("attribute-unknown", supplier("S-1", **{ident("att", 999): "x"}), [C.VALUE_ATTRIBUTE_UNKNOWN]),
    ("derived-value", part("P-1", **{SUPPLIER_COUNT: "2"}), [C.VALUE_DERIVED]),
    ("wrong-type", supplier("S-1", **{RISK_SCORE: "abc"}), [C.VALUE_TYPE]),
    ("enum-type-member", supplier("S-1", **{TIER: "9"}), [C.VALUE_TYPE]),
    ("not-canonical-integer", supplier("S-1", **{RISK_SCORE: "042"}), [C.VALUE_NOT_CANONICAL]),
    (
        "not-canonical-decimal",
        Record(
            kind=CONTRACT,
            key=CONTRACT_1,
            values={**contract(CONTRACT_1).values, CONTRACT_VALUE: "1.5"},
        ),
        [C.VALUE_NOT_CANONICAL],
    ),
    (
        "required-missing",
        Record(kind=SUPPLIER, key="S-1", values={SUPPLIER_CODE: "S-1", SUPPLIER_COUNTRY: "IT"}),
        [C.VALUE_REQUIRED],
    ),
    ("below-min", supplier("S-1", **{RISK_SCORE: "-1"}), [C.VALUE_MIN]),
    ("above-max", supplier("S-1", **{RISK_SCORE: "101"}), [C.VALUE_MAX]),
    ("too-short", supplier("S-1", **{SUPPLIER_COUNTRY: "I"}), [C.VALUE_MIN_LENGTH]),
    ("too-long", supplier("S-1", **{SUPPLIER_COUNTRY: "ITA"}), [C.VALUE_MAX_LENGTH]),
    ("pattern", supplier("X-1"), [C.VALUE_PATTERN]),
    ("array-too-long", part("P-1", **{TAGS: ["a", "b", "c", "d"]}), [C.VALUE_MAX_LENGTH]),
]


@pytest.mark.parametrize(
    ("record", "expected"), [pytest.param(r, e, id=name) for name, r, e in RECORD_CASES]
)
def test_record_rules(record: Record, expected: list[IssueCode]) -> None:
    assert codes_of(supply_schema(), record) == expected


def test_enum_values_constraint() -> None:
    schema = replace_attribute(
        supply_schema(), SUPPLIER, SUPPLIER_CITY, constraints=Constraints(enum_values=("Milan",))
    )
    assert codes_of(schema, supplier("S-1", **{SUPPLIER_CITY: "Turin"})) == [C.VALUE_ENUM_VALUES]
    assert codes_of(schema, supplier("S-1", **{SUPPLIER_CITY: "Milan"})) == []


def test_trait_required_attribute() -> None:
    schema = replace_attribute(supply_schema(), SUPPLIER, SUPPLIER_COUNTRY, required=False)
    record = Record(kind=SUPPLIER, key="S-1", values={SUPPLIER_CODE: "S-1", LEGAL_NAME: "A"})
    assert codes_of(schema, record) == [C.RECORD_TRAIT_CONFORMANCE]


def test_trait_type_conformance() -> None:
    schema = supply_schema()
    trait = schema.traits[LOCATED]
    retyped = TraitAttribute(id=LOCATED_COUNTRY, api_name="country", type=TypeSpec.of("integer"))
    schema = replace_element(
        schema, "traits", LOCATED, attributes={**trait.attributes, LOCATED_COUNTRY: retyped}
    )
    assert codes_of(schema, supplier("S-1")) == [C.RECORD_TRAIT_CONFORMANCE]
    assert codes_of(schema, supplier("S-1", **{SUPPLIER_COUNTRY: "39"})) == []


def test_issue_locates_the_value() -> None:
    (issue,) = validate_record(supply_schema(), supplier("S-1", **{RISK_SCORE: "101"}))
    assert issue.path == ("values", RISK_SCORE)
    assert issue.kind == SUPPLIER
    assert issue.key == "S-1"
    assert issue.attribute == RISK_SCORE
    assert issue.details == {"constraint": "max"}


LINK_CASES: list[tuple[str, Link, list[IssueCode]]] = [
    ("valid", link(SUPPLIED_BY, "P-1", "S-1", **{IS_PRIMARY: True}), []),
    ("relation-unknown", link(ident("rel", 99), "P-1", "S-1"), [C.LINK_RELATION_UNKNOWN]),
    ("foreign-key-backed", link(MADE_AT, "P-1", "TRN"), [C.LINK_FOREIGN_KEY_BACKED]),
    ("attribute-type", link(SUPPLIED_BY, "P-1", "S-1", **{IS_PRIMARY: "yes"}), [C.VALUE_TYPE]),
    (
        "attribute-unknown",
        link(SUPPLIED_BY, "P-1", "S-1", **{LEGAL_NAME: "x"}),
        [C.VALUE_ATTRIBUTE_UNKNOWN],
    ),
]


@pytest.mark.parametrize(
    ("item", "expected"), [pytest.param(li, e, id=n) for n, li, e in LINK_CASES]
)
def test_link_rules(item: Link, expected: list[IssueCode]) -> None:
    assert [issue.code for issue in validate_link(supply_schema(), item)] == expected


def test_link_endpoints_with_index() -> None:
    index = MemoryIndex(records=[part("P-1"), supplier("S-1")])
    schema = supply_schema()
    assert validate_link(schema, link(SUPPLIED_BY, "P-1", "S-1"), index) == []
    issues = validate_link(schema, link(SUPPLIED_BY, "P-1", "S-9"), index)
    assert [(i.code, i.path, i.key) for i in issues] == [(C.LINK_ENDPOINT_MISSING, ("to",), "S-9")]


def test_required_link_attribute() -> None:
    schema = supply_schema()
    relation = schema.relations[SUPPLIED_BY]
    required = relation.attributes[IS_PRIMARY].model_copy(update={"required": True})
    schema = replace_element(schema, "relations", SUPPLIED_BY, attributes={IS_PRIMARY: required})
    issues = validate_link(schema, link(SUPPLIED_BY, "P-1", "S-1"))
    assert [i.code for i in issues] == [C.VALUE_REQUIRED]
    assert issues[0].relation == SUPPLIED_BY
