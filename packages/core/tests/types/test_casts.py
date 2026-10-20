from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lorebase_core.types import (
    CAST_MATRIX,
    ELEMENT_RULE,
    AttributeType,
    CastError,
    CastRule,
    TypeSpec,
    cast,
    cast_rule,
    encode_value,
    is_canonical,
)

from .strategies import KIND_A, KIND_B, type_specs, values_of

T = TypeSpec.of


def test_matrix_declares_every_pair() -> None:
    for source in AttributeType:
        for target in AttributeType:
            assert (source, target) in CAST_MATRIX, (source, target)
    assert len(CAST_MATRIX) == len(AttributeType) ** 2


def test_matrix_diagonal_is_safe_or_element() -> None:
    for name in AttributeType:
        expected = ELEMENT_RULE if name is AttributeType.ARRAY else CastRule.SAFE
        assert CAST_MATRIX[(name, name)] == expected


def test_design_examples() -> None:
    assert cast_rule(T("integer"), TypeSpec.decimal(30, 2)) is CastRule.SAFE
    assert cast_rule(T("string"), T("integer")) is CastRule.CHECKED
    assert cast_rule(T("geo_point"), T("boolean")) is CastRule.FORBIDDEN


@given(st.data())
def test_cast_rules_hold_for_random_values(data: st.DataObject) -> None:
    source = data.draw(type_specs(), label="source")
    target = data.draw(type_specs(), label="target")
    value = encode_value(source, data.draw(values_of(source), label="value"))
    rule = cast_rule(source, target)
    if rule is CastRule.FORBIDDEN:
        with pytest.raises(CastError):
            cast(source, target, value)
        return
    try:
        result = cast(source, target, value)
    except CastError:
        assert rule is CastRule.CHECKED, f"safe cast failed for {value!r}"
        return
    assert is_canonical(target, result)


@given(st.data())
def test_safe_casts_never_fail(data: st.DataObject) -> None:
    source = data.draw(type_specs(), label="source")
    target = data.draw(
        type_specs().filter(lambda t: cast_rule(source, t) is CastRule.SAFE), label="target"
    )
    value = encode_value(source, data.draw(values_of(source), label="value"))
    assert is_canonical(target, cast(source, target, value))


@given(st.data())
def test_forbidden_casts_always_raise(data: st.DataObject) -> None:
    source = data.draw(type_specs(), label="source")
    target = data.draw(
        type_specs().filter(lambda t: cast_rule(source, t) is CastRule.FORBIDDEN), label="target"
    )
    value = encode_value(source, data.draw(values_of(source), label="value"))
    with pytest.raises(CastError):
        cast(source, target, value)


@given(st.data())
def test_identity_cast_returns_the_value(data: st.DataObject) -> None:
    spec = data.draw(type_specs())
    value = encode_value(spec, data.draw(values_of(spec)))
    assert cast(spec, spec, value) == value


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        (TypeSpec.decimal(12, 2), TypeSpec.decimal(14, 3), CastRule.SAFE),
        (TypeSpec.decimal(12, 2), TypeSpec.decimal(8, 2), CastRule.CHECKED),
        (TypeSpec.decimal(12, 2), TypeSpec.decimal(12, 1), CastRule.CHECKED),
        (T("integer"), TypeSpec.decimal(19, 0), CastRule.SAFE),
        (T("integer"), TypeSpec.decimal(20, 2), CastRule.CHECKED),
        (T("boolean"), TypeSpec.decimal(1, 1), CastRule.CHECKED),
        (T("boolean"), TypeSpec.decimal(2, 1), CastRule.SAFE),
        (TypeSpec.enum("a"), TypeSpec.enum("a", "b"), CastRule.SAFE),
        (TypeSpec.enum("a", "b"), TypeSpec.enum("a"), CastRule.CHECKED),
        (TypeSpec.ref(KIND_A), TypeSpec.ref(KIND_A), CastRule.SAFE),
        (TypeSpec.ref(KIND_A), TypeSpec.ref(KIND_B), CastRule.CHECKED),
        (TypeSpec.array(T("integer")), TypeSpec.array(T("string")), CastRule.SAFE),
        (TypeSpec.array(T("string")), TypeSpec.array(T("integer")), CastRule.CHECKED),
        (T("integer"), TypeSpec.array(T("integer")), CastRule.SAFE),
        (TypeSpec.array(T("integer")), T("integer"), CastRule.CHECKED),
        (TypeSpec.array(T("geo_point")), T("boolean"), CastRule.FORBIDDEN),
        (T("json"), TypeSpec.array(T("string")), CastRule.FORBIDDEN),
        (T("json"), T("string"), CastRule.FORBIDDEN),
    ],
)
def test_parameter_refinement(source: TypeSpec, target: TypeSpec, expected: CastRule) -> None:
    assert cast_rule(source, target) is expected


@pytest.mark.parametrize(
    ("source", "target", "value", "expected"),
    [
        (T("string"), T("integer"), "042", "42"),
        (T("string"), T("boolean"), "true", True),
        (T("string"), T("geo_point"), "45.1, 9.2", {"lat": 45.1, "lon": 9.2}),
        (T("string"), T("timestamp"), "2026-01-01T01:00:00+01:00", "2026-01-01T00:00:00.000000Z"),
        (T("integer"), TypeSpec.decimal(5, 2), "42", "42.00"),
        (T("integer"), T("float"), "-7", -7.0),
        (T("integer"), T("boolean"), "1", True),
        (T("integer"), T("string"), "42", "42"),
        (TypeSpec.decimal(5, 2), T("integer"), "12.00", "12"),
        (TypeSpec.decimal(5, 2), T("float"), "0.10", 0.1),
        (TypeSpec.decimal(5, 2), TypeSpec.decimal(4, 1), "12.30", "12.3"),
        (T("float"), T("integer"), 3.0, "3"),
        (T("float"), TypeSpec.decimal(5, 2), 0.25, "0.25"),
        (T("float"), T("string"), 0.1, "0.1"),
        (T("boolean"), T("integer"), False, "0"),
        (T("boolean"), T("string"), True, "true"),
        (T("date"), T("timestamp"), "2026-03-01", "2026-03-01T00:00:00.000000Z"),
        (T("timestamp"), T("date"), "2026-03-01T00:00:00.000000Z", "2026-03-01"),
        (TypeSpec.enum("1", "2"), T("integer"), "2", "2"),
        (T("string"), TypeSpec.enum("low", "high"), "low", "low"),
        (T("geo_point"), T("string"), {"lat": 1.0, "lon": 2.0}, "1.0,2.0"),
        (TypeSpec.array(T("integer")), T("integer"), [], None),
        (TypeSpec.array(T("integer")), T("integer"), ["5"], "5"),
        (T("integer"), TypeSpec.array(T("string")), "5", ["5"]),
        (TypeSpec.array(T("string")), TypeSpec.array(T("integer")), ["1", "2"], ["1", "2"]),
        (T("string"), T("integer"), None, None),
    ],
)
def test_cast_examples(source: TypeSpec, target: TypeSpec, value: object, expected: object) -> None:
    assert cast(source, target, value) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("source", "target", "value"),
    [
        (T("string"), T("integer"), "abc"),
        (T("string"), T("integer"), "9223372036854775808"),
        (T("string"), T("boolean"), "yes"),
        (T("string"), T("geo_point"), "1;2"),
        (T("string"), TypeSpec.enum("low"), "high"),
        (T("integer"), T("float"), str(2**53 + 1)),
        (T("integer"), T("boolean"), "2"),
        (T("integer"), TypeSpec.decimal(3, 1), "100"),
        (TypeSpec.decimal(5, 2), T("integer"), "1.50"),
        (TypeSpec.decimal(5, 2), TypeSpec.decimal(5, 1), "1.25"),
        (T("float"), T("integer"), 1.5),
        (T("float"), T("integer"), 1e19),
        (T("float"), TypeSpec.decimal(5, 2), 0.125),
        (T("timestamp"), T("date"), "2026-03-01T00:00:00.000001Z"),
        (TypeSpec.array(T("integer")), T("integer"), ["1", "2"]),
        (T("geo_point"), T("boolean"), {"lat": 1.0, "lon": 2.0}),
        (T("json"), T("string"), {"a": 1}),
        (T("integer"), T("string"), "not canonical"),
    ],
)
def test_cast_failures(source: TypeSpec, target: TypeSpec, value: object) -> None:
    with pytest.raises(CastError):
        cast(source, target, value)  # type: ignore[arg-type]
