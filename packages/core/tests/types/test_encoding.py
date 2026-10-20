from __future__ import annotations

import json
import unicodedata
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from lorebase_core.types import (
    INT64_MAX,
    INT64_MIN,
    AttributeType,
    EncodingError,
    EncodingReason,
    GeoPoint,
    TypeSpec,
    decode_value,
    encode_value,
    is_canonical,
    normalize_key,
    to_text,
)

from .strategies import KIND_A, type_specs, values_of

T = TypeSpec.of
DEC_5_2 = TypeSpec.decimal(5, 2)


# Round trips ------------------------------------------------------------------


@given(st.data())
def test_encode_is_idempotent_and_decode_round_trips(data: st.DataObject) -> None:
    spec = data.draw(type_specs())
    raw = data.draw(values_of(spec))
    canonical = encode_value(spec, raw)
    assert encode_value(spec, canonical) == canonical
    assert is_canonical(spec, canonical)
    assert encode_value(spec, decode_value(spec, canonical)) == canonical


@given(st.data())
def test_canonical_values_survive_json(data: st.DataObject) -> None:
    spec = data.draw(type_specs())
    canonical = encode_value(spec, data.draw(values_of(spec)))
    reloaded = json.loads(json.dumps(canonical))
    assert is_canonical(spec, reloaded)
    assert encode_value(spec, reloaded) == canonical


@given(st.integers(INT64_MIN, INT64_MAX))
def test_integer_round_trip(value: int) -> None:
    canonical = encode_value(T("integer"), value)
    assert canonical == str(value)
    assert decode_value(T("integer"), canonical) == value


@given(st.integers(-99999, 99999))
def test_decimal_round_trip(cents: int) -> None:
    value = Decimal(cents).scaleb(-2)
    canonical = encode_value(DEC_5_2, value)
    assert isinstance(canonical, str)
    assert len(canonical.split(".")[1]) == 2
    assert decode_value(DEC_5_2, canonical) == value


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_float_round_trip(value: float) -> None:
    assert decode_value(T("float"), encode_value(T("float"), value)) == value


@given(st.dates())
def test_date_round_trip(value: date) -> None:
    assert decode_value(T("date"), encode_value(T("date"), value)) == value


@given(
    st.datetimes(
        min_value=datetime(2, 1, 1),
        max_value=datetime(9998, 12, 31),
        timezones=st.sampled_from([UTC, timezone(timedelta(hours=-7))]),
    )
)
def test_timestamp_round_trip_preserves_instant(value: datetime) -> None:
    canonical = encode_value(T("timestamp"), value)
    assert isinstance(canonical, str)
    assert canonical.endswith("Z")
    assert len(canonical) == len("2026-01-01T00:00:00.000000Z")
    decoded = decode_value(T("timestamp"), canonical)
    assert decoded == value
    assert isinstance(decoded, datetime)
    assert decoded.utcoffset() == timedelta(0)


@given(st.timedeltas())
def test_duration_round_trip(value: timedelta) -> None:
    assert decode_value(T("duration"), encode_value(T("duration"), value)) == value


@given(st.text(st.characters(codec="utf-8", exclude_characters="\x00")))
def test_string_round_trip_is_nfc(value: str) -> None:
    canonical = encode_value(T("string"), value)
    assert canonical == unicodedata.normalize("NFC", value)
    assert decode_value(T("string"), canonical) == canonical


# Canonical forms --------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "raw", "expected"),
    [
        (T("integer"), "42", "42"),
        (T("integer"), "+0042", "42"),
        (T("integer"), "-0", "0"),
        (T("integer"), Decimal("7E+1"), "70"),
        (T("integer"), str(INT64_MAX), str(INT64_MAX)),
        (T("integer"), str(INT64_MIN), str(INT64_MIN)),
        (DEC_5_2, "12.5", "12.50"),
        (DEC_5_2, 12, "12.00"),
        (DEC_5_2, "1.230", "1.23"),
        (DEC_5_2, "-0.00", "0.00"),
        (DEC_5_2, "999.99", "999.99"),
        (DEC_5_2, 0.1, "0.10"),
        (DEC_5_2, "1.5e1", "15.00"),
        (TypeSpec.decimal(3, 0), "120", "120"),
        (TypeSpec.decimal(4, 4), ".5", "0.5000"),
        (T("float"), -0.0, 0.0),
        (T("float"), 3, 3.0),
        (T("float"), "2.5", 2.5),
        (T("date"), "2026-02-28", "2026-02-28"),
        (T("date"), date(5, 1, 2), "0005-01-02"),
        (T("timestamp"), "2026-10-20T09:12:44Z", "2026-10-20T09:12:44.000000Z"),
        (T("timestamp"), "2026-10-20t09:12:44.5+02:00", "2026-10-20T07:12:44.500000Z"),
        (T("timestamp"), "2026-10-20T09:12:44.123456000Z", "2026-10-20T09:12:44.123456Z"),
        (T("timestamp"), datetime(2026, 1, 1, tzinfo=UTC), "2026-01-01T00:00:00.000000Z"),
        (T("duration"), "PT90M", "P0DT1H30M0.000000S"),
        (T("duration"), "P2D", "P2DT0H0M0.000000S"),
        (T("duration"), "-PT0.5S", "-P0DT0H0M0.500000S"),
        (T("duration"), timedelta(0), "P0DT0H0M0.000000S"),
        (T("string"), "Cafe\u0301", "Caf\u00e9"),
        (T("uri"), "https://example.com/a?b=1", "https://example.com/a?b=1"),
        (TypeSpec.enum("low", "high"), "low", "low"),
        (TypeSpec.ref(KIND_A), "S-1001", "S-1001"),
        (T("geo_point"), {"lat": 45, "lon": -0.0}, {"lat": 45.0, "lon": 0.0}),
        (T("geo_point"), GeoPoint(1.5, 2.5), {"lat": 1.5, "lon": 2.5}),
        (T("json"), {"k": [1, None, "e\u0301"]}, {"k": [1, None, "\u00e9"]}),
        (TypeSpec.array(T("integer")), [1, "2"], ["1", "2"]),
        (TypeSpec.array(T("integer")), [], []),
        (T("boolean"), True, True),
    ],
)
def test_canonical_form(spec: TypeSpec, raw: object, expected: object) -> None:
    assert encode_value(spec, raw) == expected


def test_none_is_absent() -> None:
    for name in AttributeType:
        if name in (
            AttributeType.DECIMAL,
            AttributeType.ENUM,
            AttributeType.ARRAY,
            AttributeType.REF,
        ):
            continue
        assert encode_value(T(name), None) is None
        assert decode_value(T(name), None) is None


# Rejections -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "raw", "reason"),
    [
        (T("integer"), True, EncodingReason.WRONG_TYPE),
        (T("integer"), 1.0, EncodingReason.WRONG_TYPE),
        (T("integer"), "1.0", EncodingReason.FORMAT),
        (T("integer"), "1_000", EncodingReason.FORMAT),
        (T("integer"), "\uff14\uff12", EncodingReason.FORMAT),
        (T("integer"), str(INT64_MAX + 1), EncodingReason.OUT_OF_RANGE),
        (T("integer"), str(INT64_MIN - 1), EncodingReason.OUT_OF_RANGE),
        (T("integer"), INT64_MAX + 1, EncodingReason.OUT_OF_RANGE),
        (T("integer"), "9" * 5000, EncodingReason.OUT_OF_RANGE),
        (T("integer"), Decimal("1E+30"), EncodingReason.OUT_OF_RANGE),
        (T("integer"), Decimal("1.5"), EncodingReason.FORMAT),
        (DEC_5_2, "1000.00", EncodingReason.OUT_OF_RANGE),
        (DEC_5_2, "0.001", EncodingReason.PRECISION),
        (DEC_5_2, "1e999999999", EncodingReason.OUT_OF_RANGE),
        (DEC_5_2, "NaN", EncodingReason.FORMAT),
        (DEC_5_2, float("inf"), EncodingReason.OUT_OF_RANGE),
        (DEC_5_2, False, EncodingReason.WRONG_TYPE),
        (TypeSpec.decimal(2, 2), "1.00", EncodingReason.OUT_OF_RANGE),
        (T("float"), float("nan"), EncodingReason.OUT_OF_RANGE),
        (T("float"), "1e400", EncodingReason.OUT_OF_RANGE),
        (T("float"), 2**53 + 1, EncodingReason.PRECISION),
        (T("float"), True, EncodingReason.WRONG_TYPE),
        (T("boolean"), 1, EncodingReason.WRONG_TYPE),
        (T("boolean"), "true", EncodingReason.WRONG_TYPE),
        (T("date"), "2026-02-30", EncodingReason.FORMAT),
        (T("date"), "20260101", EncodingReason.FORMAT),
        (T("date"), datetime(2026, 1, 1, tzinfo=UTC), EncodingReason.WRONG_TYPE),
        (T("timestamp"), "2026-10-20T09:12:44", EncodingReason.FORMAT),
        (T("timestamp"), "2026-10-20", EncodingReason.FORMAT),
        (T("timestamp"), "2026-10-20T09:12:44.1234567Z", EncodingReason.PRECISION),
        (T("timestamp"), "2026-10-20T24:00:00Z", EncodingReason.FORMAT),
        (T("timestamp"), "2026-10-20T09:12:44+24:00", EncodingReason.FORMAT),
        (T("timestamp"), datetime(2026, 1, 1), EncodingReason.FORMAT),
        (T("timestamp"), "0001-01-01T00:00:00+01:00", EncodingReason.OUT_OF_RANGE),
        (T("duration"), "P", EncodingReason.FORMAT),
        (T("duration"), "P1DT", EncodingReason.FORMAT),
        (T("duration"), "P1Y", EncodingReason.FORMAT),
        (T("duration"), "PT0.0000001S", EncodingReason.PRECISION),
        (T("duration"), "P9999999999D", EncodingReason.OUT_OF_RANGE),
        (T("string"), "a\x00b", EncodingReason.INVALID_TEXT),
        (T("string"), "\ud800", EncodingReason.INVALID_TEXT),
        (T("string"), 5, EncodingReason.WRONG_TYPE),
        (T("uri"), "not a uri", EncodingReason.FORMAT),
        (TypeSpec.enum("low", "high"), "medium", EncodingReason.NOT_A_MEMBER),
        (TypeSpec.ref(KIND_A), "", EncodingReason.FORMAT),
        (TypeSpec.ref(KIND_A), "a\x1fb", EncodingReason.INVALID_TEXT),
        (T("geo_point"), {"lat": 91, "lon": 0}, EncodingReason.OUT_OF_RANGE),
        (T("geo_point"), {"lat": 0, "lon": 180.5}, EncodingReason.OUT_OF_RANGE),
        (T("geo_point"), {"lat": 0}, EncodingReason.FORMAT),
        (T("json"), {"a": 2**53}, EncodingReason.OUT_OF_RANGE),
        (T("json"), {"e\u0301": 1, "\u00e9": 2}, EncodingReason.FORMAT),
        (T("json"), {1: "a"}, EncodingReason.WRONG_TYPE),
        (T("json"), object(), EncodingReason.WRONG_TYPE),
        (TypeSpec.array(T("integer")), ["1", None], EncodingReason.FORMAT),
        (TypeSpec.array(T("integer")), ["x"], EncodingReason.FORMAT),
        (TypeSpec.array(T("integer")), "12", EncodingReason.WRONG_TYPE),
    ],
)
def test_rejections(spec: TypeSpec, raw: object, reason: EncodingReason) -> None:
    with pytest.raises(EncodingError) as info:
        encode_value(spec, raw)
    assert info.value.reason is reason


@pytest.mark.parametrize(
    ("spec", "value"),
    [
        (T("integer"), "042"),
        (DEC_5_2, "12.5"),
        (T("timestamp"), "2026-10-20T09:12:44Z"),
        (T("string"), "Cafe\u0301"),
    ],
)
def test_decode_rejects_non_canonical(spec: TypeSpec, value: object) -> None:
    with pytest.raises(EncodingError) as info:
        decode_value(spec, value)
    assert info.value.reason is EncodingReason.NOT_CANONICAL


# Type specs -------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "decimal"},
        {"name": "decimal", "precision": 2, "scale": 3},
        {"name": "decimal", "precision": 0, "scale": 0},
        {"name": "integer", "precision": 5, "scale": 0},
        {"name": "enum"},
        {"name": "enum", "enum_values": []},
        {"name": "enum", "enum_values": ["a", "a"]},
        {"name": "enum", "enum_values": ["e\u0301", "\u00e9"]},
        {"name": "string", "enum_values": ["a"]},
        {"name": "array"},
        {"name": "array", "element": {"name": "array", "element": {"name": "string"}}},
        {"name": "array", "element": {"name": "json"}},
        {"name": "string", "element": {"name": "string"}},
        {"name": "ref"},
        {"name": "ref", "kind_ref": "not-a-kind"},
        {"name": "string", "kind_ref": KIND_A},
        {"name": "struct"},
    ],
)
def test_invalid_type_specs(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TypeSpec.model_validate(kwargs)


def test_type_spec_describe() -> None:
    assert TypeSpec.decimal(12, 2).describe() == "decimal(12,2)"
    assert TypeSpec.array(T("string")).describe() == "array<string>"


def test_key_normalization() -> None:
    assert normalize_key("Cafe\u0301") == "Caf\u00e9"
    with pytest.raises(EncodingError):
        normalize_key("")
    with pytest.raises(EncodingError):
        normalize_key("a\nb")


@pytest.mark.parametrize(
    ("spec", "value", "text"),
    [
        (T("boolean"), True, "true"),
        (T("float"), 1.0, "1.0"),
        (T("float"), 1e16, "1e+16"),
        (T("geo_point"), {"lat": 1.0, "lon": -2.5}, "1.0,-2.5"),
        (DEC_5_2, "1.50", "1.50"),
    ],
)
def test_to_text(spec: TypeSpec, value: object, text: str) -> None:
    assert to_text(spec, value) == text  # type: ignore[arg-type]
