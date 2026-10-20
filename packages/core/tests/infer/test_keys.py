"""Primary key choice (table driven)."""

from __future__ import annotations

import pytest

from lorebase_core.infer import InferenceOptions, TableProfile
from lorebase_core.infer.tables import analyze_table

from .support import table

OPTIONS = InferenceOptions()
ULIDS = ["01JZ72C4HR2QRFWCVF58JF4E0G", "01JZA84NH8NRV3HQWNMETSSK2G", "01JZB0C3ZR46JSEFDMMFMR8NHH"]


def key_names(profile: TableProfile, options: InferenceOptions = OPTIONS) -> tuple[str, ...]:
    analysis = analyze_table(profile, options)
    if analysis.key is None:
        return ()
    return tuple(analysis.columns[i].name for i in analysis.key.columns)


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        # Ranked names: id first, then the table-named code.
        (table("supplier", name=["a", "b"], code=["1", "2"], id=["x", "y"]), ("id",)),
        (table("suppliers", name=["a", "b"], supplier_code=["S1", "S2"]), ("supplier_code",)),
        (table("purchase_order", po_number=["P1", "P2"], qty=["1", "1"]), ("po_number",)),
        # A unique column with a key name but nulls is not a candidate, and an unnamed
        # candidate is only taken when it is the first column.
        (table("t", code=["a", None, "b"], name=["x", "y", "z"]), ()),
        (table("t", code=["a", "b", "c"], name=["x", "y", "z"]), ("code",)),
        # No named candidate: the first column, when unique and never null.
        (table("t", label=["a", "b"], qty=["1", "1"]), ("label",)),
        # Composite key of reference-named columns.
        (
            table("link", from_key=["a", "a", "b"], to_key=["x", "y", "x"], w=["1", "1", "1"]),
            ("from_key", "to_key"),
        ),
        # Nothing unique: minted keys.
        (table("t", a=["1", "1"], b=["x", "x"]), ()),
        # Floats and booleans are never keys.
        (table("t", score=["0.123456", "0.654321"], flag=["true", "false"]), ()),
    ],
)
def test_key_choice(profile: TableProfile, expected: tuple[str, ...]) -> None:
    assert key_names(profile) == expected


def test_unnamed_first_column_can_be_disabled() -> None:
    profile = table("t", label=["a", "b"], qty=["1", "1"])
    assert key_names(profile, InferenceOptions(allow_unnamed_primary_key=False)) == ()


def test_key_name_patterns_are_options() -> None:
    profile = table("t", sku=["a", "b"], ean=["1", "2"])
    options = InferenceOptions(key_name_patterns=("ean",))
    assert key_names(profile, options) == ("ean",)


def test_two_named_candidates_are_listed() -> None:
    analysis = analyze_table(
        table("supplier", supplier_code=["S1", "S2"], vat_number=["V1", "V2"]), OPTIONS
    )
    assert analysis.key is not None
    assert [analysis.columns[i].name for i in analysis.key.candidates] == [
        "supplier_code",
        "vat_number",
    ]


def test_declared_primary_key_wins() -> None:
    profile = TableProfile(
        "t", table("t", a=["1", "1"], b=["x", "y"]).columns, primary_key=("a", "b")
    )
    analysis = analyze_table(profile, OPTIONS)
    assert analysis.key is not None
    assert analysis.key.declared
    assert analysis.key.confidence == 1.0
    assert analysis.key.columns == (0, 1)


def test_generated_key_column_means_minted_keys() -> None:
    analysis = analyze_table(table("risk_flag", key=ULIDS, reason=["a", "b", "c"]), OPTIONS)
    assert analysis.key is not None
    assert analysis.key.minted
    plain = analyze_table(table("risk_flag", key=["k1", "k2", "k3"]), OPTIONS)
    assert plain.key is not None
    assert not plain.key.minted


def test_sampled_tables_lower_key_confidence() -> None:
    columns = table("t", id=[str(i) for i in range(30)]).columns
    full = analyze_table(TableProfile("t", columns), OPTIONS)
    sampled = analyze_table(TableProfile("t", columns, row_count=1000), OPTIONS)
    assert full.key is not None
    assert sampled.key is not None
    assert sampled.sampled
    assert sampled.key.confidence < full.key.confidence
