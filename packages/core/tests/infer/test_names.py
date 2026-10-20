"""Name normalization rules of schema inference."""

from __future__ import annotations

import pytest

from lorebase_core.infer.names import (
    humanize,
    normalize,
    pascal,
    plural,
    singular,
    strip_prefixes,
    strip_suffix,
    unique_name,
)
from lorebase_core.model import NameStyle, is_valid_api_name


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("supplier", "supplier"),
        ("PurchaseOrders", "purchase_orders"),
        ("purchaseOrderID", "purchase_order_id"),
        ("Unit Price (EUR)", "unit_price_eur"),
        ("  po-number ", "po_number"),
        ("2nd_line", "c_2nd_line"),
        ("Città", "citta"),
        ("___", "c"),
        ("HTTPStatus", "http_status"),
    ],
)
def test_normalize(source: str, expected: str) -> None:
    assert normalize(source) == expected
    assert is_valid_api_name(normalize(source), NameStyle.SNAKE)


@pytest.mark.parametrize(
    ("word", "one"),
    [
        ("suppliers", "supplier"),
        ("categories", "category"),
        ("purchase_orders", "purchase_order"),
        ("boxes", "box"),
        ("status", "status"),
        ("address", "address"),
        ("part", "part"),
    ],
)
def test_singular(word: str, one: str) -> None:
    assert singular(word) == one


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("supplier", "suppliers"),
        ("category", "categories"),
        ("day", "days"),
        ("box", "boxes"),
        ("purchase_order", "purchase_orders"),
        ("risk_flag", "risk_flags"),
    ],
)
def test_plural(word: str, expected: str) -> None:
    assert plural(word) == expected


@pytest.mark.parametrize(
    ("snake", "expected"),
    [("purchase_order", "PurchaseOrder"), ("risk_flag", "RiskFlag"), ("c_2nd", "C2nd")],
)
def test_pascal(snake: str, expected: str) -> None:
    assert pascal(snake) == expected
    assert is_valid_api_name(pascal(snake), NameStyle.PASCAL)


def test_helpers() -> None:
    assert humanize("purchase_order") == "Purchase order"
    assert strip_prefixes("tbl_supplier", ("tbl_",)) == "supplier"
    assert strip_prefixes("tbl_", ("tbl_",)) == "tbl_"
    assert strip_suffix("supplier_id", ("_id", "_key")) == "supplier"
    assert strip_suffix("from_key", ("_id", "_key")) == "from"
    assert strip_suffix("supplier", ("_id",)) is None
    taken: set[str] = {"parts"}
    assert unique_name("parts", taken, ("parts_via_x",)) == "parts_via_x"
    assert unique_name("parts", taken, ("parts_via_x",)) == "parts_2"
