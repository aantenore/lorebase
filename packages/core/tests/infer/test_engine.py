"""The proposal as a whole: names, titles, issues, validity and determinism."""

from __future__ import annotations

import random

from hypothesis import given, settings
from hypothesis import strategies as st

from lorebase_core.infer import (
    ColumnProfile,
    InferenceIssueCode,
    InferenceOptions,
    TableProfile,
    infer_schema,
)
from lorebase_core.model import ElementStatus, dump_schema, load_schema, validate_schema
from lorebase_core.validate import SchemaRules

from .support import supply_tables, table

ULIDS = [
    "01JZ72C4HR2QRFWCVF58JF4E0G",
    "01JZA84NH8NRV3HQWNMETSSK2G",
    "01JZB0C3ZR46JSEFDMMFMR8NHH",
]


def test_kind_and_attribute_names() -> None:
    proposal = infer_schema(
        [table("PurchaseOrders", PONumber=["A1", "A2"], **{"Unit Price": ["1.5", "2"]})], "d"
    )
    [kind] = proposal.schema.kinds.values()
    assert kind.api_name == "PurchaseOrder"
    assert kind.display_name == "Purchase order"
    assert [a.api_name for a in kind.attributes.values()] == ["po_number", "unit_price"]
    assert kind.status is ElementStatus.DRAFT


def test_reserved_and_duplicate_column_names_are_renamed() -> None:
    proposal = infer_schema([table("order", id=["1", "2"], kind=["a", "b"], Kind=["c", "d"])], "d")
    [kind] = proposal.schema.kinds.values()
    assert [a.api_name for a in kind.attributes.values()] == ["id", "order_kind", "order_kind_2"]
    adjusted = [i for i in proposal.issues if i.code is InferenceIssueCode.NAME_ADJUSTED]
    assert len(adjusted) == 2


def test_minted_key_column_is_not_an_attribute() -> None:
    proposal = infer_schema([table("risk_flag", key=ULIDS, reason=["a b c d"] * 3)], "d")
    [kind] = proposal.schema.kinds.values()
    assert kind.primary_key == ()
    assert [a.api_name for a in kind.attributes.values()] == ["reason"]
    assert kind.title_attribute == next(iter(kind.attributes))


def test_title_attribute_guess() -> None:
    def title(profile: TableProfile) -> str | None:
        [kind] = infer_schema([profile], "d").schema.kinds.values()
        if kind.title_attribute is None:
            return None
        return kind.attributes[kind.title_attribute].api_name

    assert title(table("t", code=["a", "b"], legal_name=["x", "y"], label=["l", "m"])) == "label"
    assert title(table("t", code=["a", "b"], legal_name=["x", "y"])) == "legal_name"
    assert title(table("t", po_number=["a", "b"], qty=["1", "2"])) == "po_number"
    assert title(table("t", qty=["1", "2"])) is None


def test_issues_for_ambiguities() -> None:
    proposal = infer_schema(
        [
            table("supplier", supplier_code=["S1", "S2"], vat_number=["V1", "V2"]),
            table("log", level=["a", "a"], value=[str(i) for i in range(19)] + ["n/a"]),
            table("empty", a=[]),
        ],
        "d",
    )
    codes = {(i.code, i.source) for i in proposal.issues}
    assert (InferenceIssueCode.AMBIGUOUS_PRIMARY_KEY, "supplier") in codes
    assert (InferenceIssueCode.MIXED_TYPES, "log.value") in codes
    assert (InferenceIssueCode.NO_PRIMARY_KEY, "log") in codes
    assert (InferenceIssueCode.EMPTY_TABLE, "empty") in codes


def test_sampled_tables_are_reported() -> None:
    profile = TableProfile("t", (ColumnProfile("id", ["1", "2"]),), row_count=10)
    proposal = infer_schema([profile], "d")
    assert InferenceIssueCode.SAMPLED in {i.code for i in proposal.issues}
    assert proposal.tables[0].rows == 10
    assert proposal.tables[0].values_read == 2


def test_evidence_covers_every_element() -> None:
    proposal = infer_schema(supply_tables(), "supply")
    elements = {e.element_id for e in proposal.evidence}
    schema = proposal.schema
    expected = set(schema.kinds) | set(schema.relations)
    for owner in (*schema.kinds.values(), *schema.relations.values()):
        expected |= set(owner.attributes)
    assert elements == expected
    assert all(0 <= e.confidence <= 1 for e in proposal.evidence)


def test_proposal_is_valid_and_round_trips_through_yaml() -> None:
    proposal = infer_schema(supply_tables(), "supply")
    assert validate_schema(proposal.schema, SchemaRules()) == []
    assert not [i for i in proposal.issues if i.code is InferenceIssueCode.SCHEMA_INVALID]
    assert load_schema(dump_schema(proposal.schema)) == proposal.schema
    document = proposal.document()
    assert set(document) == {"schema", "evidence", "issues", "tables"}


def test_id_seed_changes_ids_only() -> None:
    a = infer_schema(supply_tables(), "supply")
    b = infer_schema(supply_tables(), "supply", InferenceOptions(id_seed="other"))
    assert set(a.schema.kinds).isdisjoint(b.schema.kinds)
    assert sorted(k.api_name for k in a.schema.kinds.values()) == sorted(
        k.api_name for k in b.schema.kinds.values()
    )


# Determinism -------------------------------------------------------------------------------

_cell = st.one_of(
    st.none(),
    st.sampled_from(["", "a", "b", "S-1", "S-2", "1", "2", "2.5", "true", "2024-01-02"]),
    st.text(alphabet="abcXYZ019-. ", max_size=8),
)
_names = st.lists(
    st.text(alphabet="abcdeIDK_ ", min_size=1, max_size=6), min_size=1, max_size=4, unique=True
)


@st.composite
def _tables(draw: st.DrawFn) -> list[TableProfile]:
    count = draw(st.integers(min_value=1, max_value=3))
    tables = []
    for index in range(count):
        columns = draw(_names)
        height = draw(st.integers(min_value=0, max_value=6))
        tables.append(
            TableProfile(
                f"t{index}",
                tuple(
                    ColumnProfile(name, draw(st.lists(_cell, min_size=height, max_size=height)))
                    for name in columns
                ),
            )
        )
    return tables


@settings(max_examples=60, deadline=None)
@given(_tables(), st.randoms(use_true_random=False))
def test_same_input_same_proposal_in_any_table_order(
    tables: list[TableProfile], rnd: random.Random
) -> None:
    first = infer_schema(tables, "d")
    shuffled = list(tables)
    rnd.shuffle(shuffled)
    second = infer_schema(shuffled, "d")
    assert first.schema == second.schema
    assert dump_schema(first.schema) == dump_schema(second.schema)
    assert first.evidence == second.evidence
    assert first.issues == second.issues
    assert not [i for i in first.issues if i.code is InferenceIssueCode.SCHEMA_INVALID]


def test_supply_proposal_is_stable() -> None:
    assert dump_schema(infer_schema(supply_tables(), "supply").schema) == dump_schema(
        infer_schema(list(reversed(supply_tables())), "supply").schema
    )
