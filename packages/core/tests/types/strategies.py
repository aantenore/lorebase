"""Hypothesis strategies for type specs and valid API values of each spec."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from hypothesis import strategies as st

from lorebase_core.types import INT64_MAX, INT64_MIN, AttributeType, TypeSpec
from lorebase_core.types.types import JSON_SAFE_INTEGER_MAX, NON_ELEMENT_TYPES

KIND_A = "knd_01JB8Z0000000000000000000A"
KIND_B = "knd_01JB8Z0000000000000000000B"

_TEXT_ALPHABET = st.characters(codec="utf-8", exclude_characters="\x00")
_KEY_ALPHABET = st.characters(codec="utf-8", exclude_categories=["Cc"])
_ENUM_POOL = ["a", "b", "c", "d", "e"]
_OFFSETS = [UTC, timezone(timedelta(hours=2)), timezone(-timedelta(hours=5, minutes=30))]

_SIMPLE = [
    AttributeType.STRING,
    AttributeType.TEXT,
    AttributeType.URI,
    AttributeType.INTEGER,
    AttributeType.FLOAT,
    AttributeType.BOOLEAN,
    AttributeType.DATE,
    AttributeType.TIMESTAMP,
    AttributeType.DURATION,
    AttributeType.JSON,
    AttributeType.GEO_POINT,
]


@st.composite
def decimal_specs(draw: st.DrawFn) -> TypeSpec:
    precision = draw(st.integers(1, 30))
    scale = draw(st.integers(0, precision))
    return TypeSpec.decimal(precision, scale)


@st.composite
def enum_specs(draw: st.DrawFn) -> TypeSpec:
    values = draw(st.lists(st.sampled_from(_ENUM_POOL), min_size=1, max_size=5, unique=True))
    return TypeSpec.enum(*values)


def scalar_specs() -> st.SearchStrategy[TypeSpec]:
    return st.one_of(
        st.sampled_from(_SIMPLE).map(TypeSpec.of),
        decimal_specs(),
        enum_specs(),
        st.sampled_from([KIND_A, KIND_B]).map(TypeSpec.ref),
    )


def type_specs() -> st.SearchStrategy[TypeSpec]:
    element = scalar_specs().filter(lambda spec: spec.name not in NON_ELEMENT_TYPES)
    return st.one_of(scalar_specs(), element.map(TypeSpec.array))


def _json_values() -> st.SearchStrategy[object]:
    leaves = st.one_of(
        st.none(),
        st.booleans(),
        st.integers(-JSON_SAFE_INTEGER_MAX, JSON_SAFE_INTEGER_MAX),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(_TEXT_ALPHABET, max_size=10),
    )
    keys = st.text("abcdefghij", min_size=1, max_size=5)
    return st.recursive(
        leaves,
        lambda children: (
            st.lists(children, max_size=4) | st.dictionaries(keys, children, max_size=4)
        ),
        max_leaves=10,
    )


def values_of(spec: TypeSpec) -> st.SearchStrategy[object]:
    """Valid API values (Python natives) of ``spec``."""
    name = spec.name
    if name in (AttributeType.STRING, AttributeType.TEXT):
        return st.text(_TEXT_ALPHABET, max_size=20)
    if name is AttributeType.URI:
        return st.from_regex(r"[a-z][a-z0-9+.\-]{0,5}:[!-~]{0,12}", fullmatch=True)
    if name is AttributeType.ENUM:
        assert spec.enum_values is not None
        return st.sampled_from(spec.enum_values)
    if name is AttributeType.INTEGER:
        return st.integers(INT64_MIN, INT64_MAX)
    if name is AttributeType.DECIMAL:
        precision, scale = spec.precision, spec.scale
        assert precision is not None
        assert scale is not None
        limit = 10**precision - 1
        return st.integers(-limit, limit).map(lambda n: Decimal(f"{n}E-{scale}"))
    if name is AttributeType.FLOAT:
        return st.floats(allow_nan=False, allow_infinity=False)
    if name is AttributeType.BOOLEAN:
        return st.booleans()
    if name is AttributeType.DATE:
        return st.dates()
    if name is AttributeType.TIMESTAMP:
        return st.datetimes(
            min_value=datetime(2, 1, 1),
            max_value=datetime(9998, 12, 31),
            timezones=st.sampled_from(_OFFSETS),
        )
    if name is AttributeType.DURATION:
        return st.timedeltas()
    if name is AttributeType.JSON:
        return _json_values()
    if name is AttributeType.GEO_POINT:
        return st.tuples(
            st.floats(-90, 90, allow_nan=False),
            st.floats(-180, 180, allow_nan=False),
        )
    if name is AttributeType.REF:
        return st.text(_KEY_ALPHABET, min_size=1, max_size=12)
    assert spec.element is not None
    return st.lists(values_of(spec.element), max_size=4)
