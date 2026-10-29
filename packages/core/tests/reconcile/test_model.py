"""Data model of reconciliation (design 10.6): the `valid_time` flag, validity periods on
provenance and the `reconciliation` origin."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from lorebase_core.model import (
    OriginType,
    ProvenanceEntry,
    ReconciliationOrigin,
    validate_schema,
)
from lorebase_core.validate import IssueCode

from ..model.supply import (
    PART,
    SUPPLIER,
    SUPPLIER_CITY,
    SUPPLIER_COUNT,
    ident,
    replace_attribute,
    supply_schema,
)

AT = "2026-10-20T09:00:00Z"


def entry(**changes: Any) -> ProvenanceEntry:
    return ProvenanceEntry.model_validate(
        {
            "id": ident("prv", 1),
            "origin": {"type": "import", "batch_id": "b1"},
            "actor": "usr_1",
            "at": AT,
            **changes,
        }
    )


def test_valid_time_is_left_out_of_content_until_set() -> None:
    schema = supply_schema()
    attribute = schema.kinds[SUPPLIER].attributes[SUPPLIER_CITY]
    assert "valid_time" not in attribute.content()
    assert not attribute.has_valid_time
    flagged = attribute.model_copy(update={"valid_time": True})
    assert flagged.content()["valid_time"] is True
    assert flagged.has_valid_time


def test_schema_hash_unchanged_without_the_flag() -> None:
    schema = supply_schema()
    same = replace_attribute(schema, SUPPLIER, SUPPLIER_CITY, valid_time=None)
    assert same.content() == schema.content()
    flagged = replace_attribute(schema, SUPPLIER, SUPPLIER_CITY, valid_time=True)
    assert flagged.content() != schema.content()
    assert validate_schema(flagged) == []


def test_derived_attributes_cannot_carry_periods() -> None:
    schema = replace_attribute(supply_schema(), PART, SUPPLIER_COUNT, valid_time=True)
    found = [
        i.path for i in validate_schema(schema) if i.code is IssueCode.SCHEMA_ATTRIBUTE_FLAG_INVALID
    ]
    assert found
    assert all(path[-1] == "valid_time" for path in found)


def test_provenance_period_is_optional_and_canonical() -> None:
    plain = entry()
    assert "valid_from" not in plain.content()
    assert "valid_to" not in plain.content()
    dated = entry(valid_from="2026-01-01T00:00:00+01:00", valid_to=None)
    assert dated.valid_from == "2025-12-31T23:00:00.000000Z"
    assert dated.content()["valid_from"] == dated.valid_from
    assert "valid_to" not in dated.content()


def test_provenance_period_must_not_end_before_it_starts() -> None:
    with pytest.raises(ValidationError, match="valid_to"):
        entry(valid_from="2026-02-01T00:00:00Z", valid_to="2026-01-01T00:00:00Z")


def test_reconciliation_origin() -> None:
    found = entry(
        origin={
            "type": "reconciliation",
            "candidate_id": "rcn_1",
            "claim_id": "prv_x",
            "resolver": "policy:authority",
            "source": "source:src_1",
        }
    )
    assert isinstance(found.origin, ReconciliationOrigin)
    assert found.origin_type is OriginType.RECONCILIATION
    custom = ReconciliationOrigin(candidate_id="rcn_1", resolver="principal:usr_1")
    assert custom.content() == {
        "type": "reconciliation",
        "candidate_id": "rcn_1",
        "resolver": "principal:usr_1",
    }


@pytest.mark.parametrize("resolver", ["usr_1", "principal:", "policy:", "source:src_1"])
def test_reconciliation_resolver_names_a_principal_or_a_policy(resolver: str) -> None:
    with pytest.raises(ValidationError, match="resolver"):
        ReconciliationOrigin(candidate_id="rcn_1", resolver=resolver)
