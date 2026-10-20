from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lorebase_core.canonical import CanonicalError, format_number, jcs_bytes

VECTORS = Path(__file__).parent / "vectors" / "rfc8785"
STRUCTURE_VECTORS = sorted(p.name.removesuffix(".input.json") for p in VECTORS.glob("*.input.json"))
NUMBER_VECTORS = json.loads((VECTORS / "numbers.json").read_text(encoding="utf-8"))


def _double(bits: str) -> float:
    value: float = struct.unpack(">d", bytes.fromhex(bits))[0]
    return value


@pytest.mark.parametrize("name", STRUCTURE_VECTORS)
def test_rfc8785_structure_vectors(name: str) -> None:
    source = (VECTORS / f"{name}.input.json").read_text(encoding="utf-8")
    expected = (VECTORS / f"{name}.expected.json").read_bytes().rstrip(b"\n")
    assert jcs_bytes(json.loads(source)) == expected


@pytest.mark.parametrize(("bits", "expected"), NUMBER_VECTORS["valid"])
def test_rfc8785_number_vectors(bits: str, expected: str) -> None:
    assert format_number(_double(bits)) == expected


@pytest.mark.parametrize("bits", NUMBER_VECTORS["invalid"])
def test_non_finite_numbers_rejected(bits: str) -> None:
    with pytest.raises(CanonicalError):
        jcs_bytes([_double(bits)])


def test_integers_beyond_double_precision_rejected() -> None:
    assert jcs_bytes(2**53) == b"9007199254740992"
    assert jcs_bytes(2**53 + 2) == b"9007199254740994"
    with pytest.raises(CanonicalError):
        jcs_bytes(2**53 + 1)
    with pytest.raises(CanonicalError):
        jcs_bytes(10**400)


@pytest.mark.parametrize("value", [b"raw", {1: "x"}, {1.5}, object()])
def test_unsupported_values_rejected(value: object) -> None:
    with pytest.raises(CanonicalError):
        jcs_bytes(value)


def test_lone_surrogate_rejected() -> None:
    with pytest.raises(CanonicalError):
        jcs_bytes("\ud800")
    with pytest.raises(CanonicalError):
        jcs_bytes({"\ud800": 1, "a": 2})


def test_control_characters_escaped() -> None:
    assert jcs_bytes("\x00\x1f\x7f") == b'"\\u0000\\u001f\x7f"'


json_scalars = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**53), max_value=2**53)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text()
)
json_values = st.recursive(
    json_scalars,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=6), children, max_size=4)
    ),
    max_leaves=20,
)


@given(json_values)
def test_round_trip_through_json_is_stable(value: object) -> None:
    once = jcs_bytes(value)
    assert jcs_bytes(json.loads(once)) == once


@given(st.dictionaries(st.text(max_size=6), json_scalars, max_size=8), st.randoms())
def test_key_order_does_not_matter(mapping: dict[str, object], rnd: object) -> None:
    items = list(mapping.items())
    rnd.shuffle(items)  # type: ignore[attr-defined]
    assert jcs_bytes(dict(items)) == jcs_bytes(mapping)


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_number_text_parses_back_to_same_double(value: float) -> None:
    parsed = float(format_number(value))
    assert parsed == value or (value == 0 and parsed == 0)
    assert not math.isnan(parsed)
