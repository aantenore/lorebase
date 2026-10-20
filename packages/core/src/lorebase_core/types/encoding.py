"""Canonical value encoding (design 6.1).

This module is the single source of truth for how a value of each Attribute type is
represented in canonical content:

- ``integer`` and ``decimal``: JSON strings; decimals normalized to the declared scale.
- ``float``: a finite JSON number (negative zero becomes zero).
- ``timestamp``: RFC 3339 UTC with exactly six fractional digits and a ``Z`` suffix.
- ``date``: ``YYYY-MM-DD``.
- ``duration``: ``[-]P{d}DT{h}H{m}M{s}.{ffffff}S`` with every component present.
- ``geo_point``: ``{"lat": float, "lon": float}``.
- strings: NFC-normalized; NUL and unpaired surrogates are rejected.
- ``json``: any JSON value with NFC strings and integers within the IEEE 754 exact range.
- ``array<T>``: an ordered list of canonical ``T`` values (no nulls).
- null and absent are the same: ``None`` encodes to ``None`` (omitted from content).

``encode_value`` accepts API values (Python natives or their string forms) and returns the
canonical value. ``decode_value`` turns a canonical value back into a Python native.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, DecimalException, localcontext

from lorebase_core.types.errors import EncodingError, EncodingReason, TypeSystemError
from lorebase_core.types.types import (
    INT64_MAX,
    INT64_MIN,
    JSON_SAFE_INTEGER_MAX,
    AttributeType,
    CanonicalValue,
    GeoPoint,
    TypeSpec,
)

_INTEGER_RE = re.compile(r"[+-]?[0-9]+")
_NUMBER_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIMESTAMP_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]+))?([Zz]|[+-][0-9]{2}:[0-9]{2})"
)
_DURATION_RE = re.compile(
    r"(-)?P(?:([0-9]+)D)?(?:T(?:([0-9]+)H)?(?:([0-9]+)M)?(?:([0-9]+)(?:\.([0-9]+))?S)?)?"
)
_URI_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:\S*")

_MICROS_PER_SECOND = 1_000_000
_MICROS_PER_MINUTE = 60 * _MICROS_PER_SECOND
_MICROS_PER_HOUR = 60 * _MICROS_PER_MINUTE
_MICROS_PER_DAY = 24 * _MICROS_PER_HOUR
_FRACTION_DIGITS = 6
#: Digits beyond which an integer string cannot be an int64 (avoids parsing huge strings).
_INT64_MAX_DIGITS = len(str(INT64_MAX))
_GEO_LAT_RANGE = (-90.0, 90.0)
_GEO_LON_RANGE = (-180.0, 180.0)


def _fail(spec_or_name: TypeSpec | str, reason: EncodingReason, message: str) -> EncodingError:
    name = spec_or_name.describe() if isinstance(spec_or_name, TypeSpec) else spec_or_name
    return EncodingError(name, reason, message)


# Text -------------------------------------------------------------------------


def normalize_text(raw: object, type_name: str = "string") -> str:
    """NFC-normalize a string; reject non-strings, NUL characters and unpaired surrogates."""
    if not isinstance(raw, str):
        raise _fail(
            type_name, EncodingReason.WRONG_TYPE, f"expected a string, got {type(raw).__name__}"
        )
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _fail(
            type_name, EncodingReason.INVALID_TEXT, "string contains unpaired surrogates"
        ) from exc
    if "\x00" in raw:
        raise _fail(type_name, EncodingReason.INVALID_TEXT, "string contains a NUL character")
    return unicodedata.normalize("NFC", raw)


def normalize_key(raw: object, type_name: str = "key") -> str:
    """Normalize a Record key (design 5.1): non-empty NFC text without control characters."""
    text = normalize_text(raw, type_name)
    if text == "":
        raise _fail(type_name, EncodingReason.FORMAT, "key must not be empty")
    if any(unicodedata.category(ch) == "Cc" for ch in text):
        raise _fail(
            type_name, EncodingReason.INVALID_TEXT, "key must not contain control characters"
        )
    return text


# Numbers ----------------------------------------------------------------------


def _encode_integer(spec: TypeSpec, raw: object) -> str:
    if isinstance(raw, bool):
        raise _fail(spec, EncodingReason.WRONG_TYPE, "booleans are not integers")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        if not _INTEGER_RE.fullmatch(raw):
            raise _fail(spec, EncodingReason.FORMAT, f"not an integer: {raw!r}")
        if len(raw.lstrip("+-").lstrip("0")) > _INT64_MAX_DIGITS:
            raise _fail(spec, EncodingReason.OUT_OF_RANGE, "integer outside the int64 range")
        value = int(raw)
    elif isinstance(raw, Decimal):
        if not raw.is_finite() or raw != raw.to_integral_value():
            raise _fail(spec, EncodingReason.FORMAT, f"not an integer: {raw}")
        if not raw.is_zero() and raw.adjusted() >= _INT64_MAX_DIGITS:
            raise _fail(spec, EncodingReason.OUT_OF_RANGE, "integer outside the int64 range")
        value = int(raw)
    else:
        raise _fail(
            spec, EncodingReason.WRONG_TYPE, f"expected an integer, got {type(raw).__name__}"
        )
    if not INT64_MIN <= value <= INT64_MAX:
        raise _fail(spec, EncodingReason.OUT_OF_RANGE, "integer outside the int64 range")
    return str(value)


def to_decimal(raw: object, type_name: str = "decimal") -> Decimal:
    """Parse a number (int, float, Decimal or numeric string) into an exact finite Decimal."""
    if isinstance(raw, bool):
        raise _fail(type_name, EncodingReason.WRONG_TYPE, "booleans are not numbers")
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        if not math.isfinite(raw):
            raise _fail(type_name, EncodingReason.OUT_OF_RANGE, "number must be finite")
        return Decimal(repr(raw))
    if isinstance(raw, Decimal):
        if not raw.is_finite():
            raise _fail(type_name, EncodingReason.OUT_OF_RANGE, "number must be finite")
        return raw
    if isinstance(raw, str):
        if not _NUMBER_RE.fullmatch(raw):
            raise _fail(type_name, EncodingReason.FORMAT, f"not a number: {raw!r}")
        try:
            return Decimal(raw)
        except DecimalException as exc:
            raise _fail(type_name, EncodingReason.FORMAT, f"not a number: {raw!r}") from exc
    raise _fail(
        type_name, EncodingReason.WRONG_TYPE, f"expected a number, got {type(raw).__name__}"
    )


def _encode_decimal(spec: TypeSpec, raw: object) -> str:
    precision, scale = spec.precision, spec.scale
    assert precision is not None
    assert scale is not None
    value = to_decimal(raw, spec.describe())
    if not value.is_zero() and value.adjusted() + 1 > precision - scale:
        raise _fail(
            spec,
            EncodingReason.OUT_OF_RANGE,
            f"{value} has more than {precision - scale} integer digits",
        )
    with localcontext() as ctx:
        ctx.prec = max(precision, len(value.as_tuple().digits)) + 2
        quantized = value.quantize(Decimal(1).scaleb(-scale))
    if quantized != value:
        raise _fail(
            spec, EncodingReason.PRECISION, f"{value} has more than {scale} fractional digits"
        )
    if quantized.is_zero():
        quantized = abs(quantized)
    return format(quantized, "f")


def _encode_float(spec: TypeSpec | str, raw: object) -> float:
    if isinstance(raw, bool):
        raise _fail(spec, EncodingReason.WRONG_TYPE, "booleans are not numbers")
    if isinstance(raw, float):
        value = raw
    elif isinstance(raw, int):
        try:
            value = float(raw)
        except OverflowError as exc:
            raise _fail(spec, EncodingReason.OUT_OF_RANGE, "integer too large for float") from exc
        if int(value) != raw:
            raise _fail(
                spec, EncodingReason.PRECISION, f"{raw} is not exactly representable as float"
            )
    elif isinstance(raw, Decimal | str):
        name = spec.describe() if isinstance(spec, TypeSpec) else spec
        value = float(to_decimal(raw, name))
    else:
        raise _fail(spec, EncodingReason.WRONG_TYPE, f"expected a number, got {type(raw).__name__}")
    if not math.isfinite(value):
        raise _fail(spec, EncodingReason.OUT_OF_RANGE, "float must be finite")
    if value == 0.0:
        value = 0.0
    return value


# Dates and times --------------------------------------------------------------


def format_date(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}-{value.day:02d}"


def format_timestamp(value: datetime) -> str:
    """Format an aware datetime as canonical RFC 3339 UTC with six fractional digits."""
    utc = value.astimezone(UTC)
    clock = f"{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}.{utc.microsecond:06d}"
    return f"{format_date(utc)}T{clock}Z"


def format_duration(value: timedelta) -> str:
    total = (value.days * 86_400 + value.seconds) * _MICROS_PER_SECOND + value.microseconds
    sign = "-" if total < 0 else ""
    rest = abs(total)
    days, rest = divmod(rest, _MICROS_PER_DAY)
    hours, rest = divmod(rest, _MICROS_PER_HOUR)
    minutes, rest = divmod(rest, _MICROS_PER_MINUTE)
    seconds, micros = divmod(rest, _MICROS_PER_SECOND)
    return f"{sign}P{days}DT{hours}H{minutes}M{seconds}.{micros:06d}S"


def _parse_fraction(spec: TypeSpec, digits: str | None) -> int:
    if not digits:
        return 0
    if len(digits) > _FRACTION_DIGITS and digits[_FRACTION_DIGITS:].strip("0"):
        raise _fail(spec, EncodingReason.PRECISION, "precision finer than microseconds")
    return int(digits[:_FRACTION_DIGITS].ljust(_FRACTION_DIGITS, "0"))


def _encode_date(spec: TypeSpec, raw: object) -> str:
    if isinstance(raw, datetime):
        raise _fail(spec, EncodingReason.WRONG_TYPE, "expected a date, got a datetime")
    if isinstance(raw, date):
        return format_date(raw)
    if not isinstance(raw, str):
        raise _fail(spec, EncodingReason.WRONG_TYPE, f"expected a date, got {type(raw).__name__}")
    if not _DATE_RE.fullmatch(raw):
        raise _fail(spec, EncodingReason.FORMAT, f"not a YYYY-MM-DD date: {raw!r}")
    try:
        return format_date(date.fromisoformat(raw))
    except ValueError as exc:
        raise _fail(spec, EncodingReason.FORMAT, f"invalid date: {raw!r}") from exc


def _parse_timestamp(spec: TypeSpec, raw: str) -> datetime:
    match = _TIMESTAMP_RE.fullmatch(raw)
    if match is None:
        raise _fail(spec, EncodingReason.FORMAT, f"not an RFC 3339 timestamp with offset: {raw!r}")
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    micros = _parse_fraction(spec, match.group(7))
    offset_text = match.group(8)
    if offset_text in ("Z", "z"):
        tz = UTC
    else:
        offset_hours, offset_minutes = int(offset_text[1:3]), int(offset_text[4:6])
        if offset_hours > 23 or offset_minutes > 59:
            raise _fail(spec, EncodingReason.FORMAT, f"invalid UTC offset: {offset_text}")
        delta = timedelta(hours=offset_hours, minutes=offset_minutes)
        tz = timezone(-delta if offset_text[0] == "-" else delta)
    try:
        return datetime(year, month, day, hour, minute, second, micros, tzinfo=tz)
    except ValueError as exc:
        raise _fail(spec, EncodingReason.FORMAT, f"invalid timestamp: {raw!r}") from exc


def _encode_timestamp(spec: TypeSpec, raw: object) -> str:
    if isinstance(raw, datetime):
        value = raw
        if value.tzinfo is None or value.utcoffset() is None:
            raise _fail(spec, EncodingReason.FORMAT, "timestamp must carry a UTC offset")
    elif isinstance(raw, str):
        value = _parse_timestamp(spec, raw)
    else:
        raise _fail(
            spec, EncodingReason.WRONG_TYPE, f"expected a timestamp, got {type(raw).__name__}"
        )
    try:
        return format_timestamp(value)
    except OverflowError as exc:
        raise _fail(
            spec, EncodingReason.OUT_OF_RANGE, "timestamp outside the supported range in UTC"
        ) from exc


def _parse_duration(spec: TypeSpec, raw: str) -> timedelta:
    match = _DURATION_RE.fullmatch(raw)
    if match is None or all(match.group(i) is None for i in range(2, 6)):
        raise _fail(
            spec, EncodingReason.FORMAT, f"not an ISO 8601 duration (days to seconds): {raw!r}"
        )
    if "T" in raw and all(match.group(i) is None for i in range(3, 6)):
        raise _fail(spec, EncodingReason.FORMAT, f"duration has an empty time part: {raw!r}")
    parts = [match.group(i) or "0" for i in range(2, 6)]
    if any(len(part) > _INT64_MAX_DIGITS for part in parts):
        raise _fail(spec, EncodingReason.OUT_OF_RANGE, "duration outside the supported range")
    days, hours, minutes, seconds = (int(part) for part in parts)
    total = (
        days * _MICROS_PER_DAY
        + hours * _MICROS_PER_HOUR
        + minutes * _MICROS_PER_MINUTE
        + seconds * _MICROS_PER_SECOND
        + _parse_fraction(spec, match.group(6))
    )
    if match.group(1):
        total = -total
    try:
        return timedelta(microseconds=total)
    except OverflowError as exc:
        raise _fail(
            spec, EncodingReason.OUT_OF_RANGE, "duration outside the supported range"
        ) from exc


def _encode_duration(spec: TypeSpec, raw: object) -> str:
    if isinstance(raw, timedelta):
        return format_duration(raw)
    if isinstance(raw, str):
        return format_duration(_parse_duration(spec, raw))
    raise _fail(spec, EncodingReason.WRONG_TYPE, f"expected a duration, got {type(raw).__name__}")


# Structured values ------------------------------------------------------------


def _encode_geo_point(spec: TypeSpec, raw: object) -> dict[str, CanonicalValue]:
    if isinstance(raw, Mapping):
        if set(raw.keys()) != {"lat", "lon"}:
            raise _fail(spec, EncodingReason.FORMAT, "geo_point needs exactly the keys lat and lon")
        lat_raw, lon_raw = raw["lat"], raw["lon"]
    elif isinstance(raw, list | tuple) and len(raw) == 2:
        lat_raw, lon_raw = raw
    else:
        raise _fail(spec, EncodingReason.WRONG_TYPE, "expected {lat, lon} or a (lat, lon) pair")
    lat = _encode_float(spec, lat_raw)
    lon = _encode_float(spec, lon_raw)
    if not _GEO_LAT_RANGE[0] <= lat <= _GEO_LAT_RANGE[1]:
        raise _fail(spec, EncodingReason.OUT_OF_RANGE, f"latitude {lat} outside [-90, 90]")
    if not _GEO_LON_RANGE[0] <= lon <= _GEO_LON_RANGE[1]:
        raise _fail(spec, EncodingReason.OUT_OF_RANGE, f"longitude {lon} outside [-180, 180]")
    return {"lat": lat, "lon": lon}


def _encode_json(spec: TypeSpec, raw: object) -> CanonicalValue:
    if raw is None or isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        if abs(raw) > JSON_SAFE_INTEGER_MAX:
            raise _fail(
                spec, EncodingReason.OUT_OF_RANGE, "json integers must be within +/-(2^53 - 1)"
            )
        return raw
    if isinstance(raw, float):
        return _encode_float(spec, raw)
    if isinstance(raw, str):
        return normalize_text(raw, spec.describe())
    if isinstance(raw, list | tuple):
        return [_encode_json(spec, item) for item in raw]
    if isinstance(raw, Mapping):
        result: dict[str, CanonicalValue] = {}
        for key, item in raw.items():
            norm_key = normalize_text(key, spec.describe())
            if norm_key in result:
                raise _fail(
                    spec,
                    EncodingReason.FORMAT,
                    f"duplicate object key after normalization: {norm_key!r}",
                )
            result[norm_key] = _encode_json(spec, item)
        return result
    raise _fail(spec, EncodingReason.WRONG_TYPE, f"not a JSON value: {type(raw).__name__}")


def _encode_array(spec: TypeSpec, raw: object) -> list[CanonicalValue]:
    if not isinstance(raw, list | tuple):
        raise _fail(spec, EncodingReason.WRONG_TYPE, f"expected a list, got {type(raw).__name__}")
    element = spec.scalar
    result: list[CanonicalValue] = []
    for index, item in enumerate(raw):
        if item is None:
            raise _fail(
                spec, EncodingReason.FORMAT, f"element {index} is null; arrays hold no nulls"
            )
        try:
            result.append(encode_value(element, item))
        except EncodingError as exc:
            raise EncodingError(
                spec.describe(), exc.reason, f"element {index}: {exc.message}"
            ) from exc
    return result


def _encode_enum(spec: TypeSpec, raw: object) -> str:
    text = normalize_text(raw, spec.describe())
    assert spec.enum_values is not None
    if text not in spec.enum_values:
        raise _fail(
            spec, EncodingReason.NOT_A_MEMBER, f"{text!r} is not one of {list(spec.enum_values)}"
        )
    return text


def _encode_uri(spec: TypeSpec, raw: object) -> str:
    text = normalize_text(raw, spec.describe())
    if not _URI_RE.fullmatch(text):
        raise _fail(spec, EncodingReason.FORMAT, f"not an absolute URI: {text!r}")
    return text


def _encode_boolean(spec: TypeSpec, raw: object) -> bool:
    if not isinstance(raw, bool):
        raise _fail(
            spec, EncodingReason.WRONG_TYPE, f"expected a boolean, got {type(raw).__name__}"
        )
    return raw


_ENCODERS: dict[AttributeType, Callable[[TypeSpec, object], CanonicalValue]] = {
    AttributeType.STRING: lambda spec, raw: normalize_text(raw, spec.describe()),
    AttributeType.TEXT: lambda spec, raw: normalize_text(raw, spec.describe()),
    AttributeType.URI: _encode_uri,
    AttributeType.ENUM: _encode_enum,
    AttributeType.INTEGER: _encode_integer,
    AttributeType.DECIMAL: _encode_decimal,
    AttributeType.FLOAT: _encode_float,
    AttributeType.BOOLEAN: _encode_boolean,
    AttributeType.DATE: _encode_date,
    AttributeType.TIMESTAMP: _encode_timestamp,
    AttributeType.DURATION: _encode_duration,
    AttributeType.JSON: _encode_json,
    AttributeType.GEO_POINT: _encode_geo_point,
    AttributeType.REF: lambda spec, raw: normalize_key(raw, spec.describe()),
    AttributeType.ARRAY: _encode_array,
}


def encode_value(spec: TypeSpec, raw: object) -> CanonicalValue:
    """Encode an API value into the canonical value of ``spec``.

    ``None`` means absent and encodes to ``None``. Raises ``EncodingError`` when the value
    is not valid for the type. Encoding is idempotent: encoding a canonical value returns it.
    """
    if raw is None:
        return None
    return _ENCODERS[spec.name](spec, raw)


def is_canonical(spec: TypeSpec, value: object) -> bool:
    """True when ``value`` is a valid canonical value of ``spec`` (``None`` included)."""
    try:
        return encode_value(spec, value) == value
    except EncodingError:
        return False


def decode_value(spec: TypeSpec, value: object) -> object:
    """Decode a canonical value of ``spec`` into a Python native value.

    ``integer`` -> ``int``, ``decimal`` -> ``Decimal``, ``float`` -> ``float``, ``date`` ->
    ``date``, ``timestamp`` -> aware ``datetime`` in UTC, ``duration`` -> ``timedelta``,
    ``geo_point`` -> ``GeoPoint``, ``array`` -> ``list``; text types and ``json`` decode to
    themselves. Raises ``EncodingError`` with reason ``not_canonical`` for non-canonical input.
    """
    if value is None:
        return None
    canonical = encode_value(spec, value)
    if canonical != value:
        raise _fail(
            spec, EncodingReason.NOT_CANONICAL, f"value is not in canonical form: {value!r}"
        )
    return _decode_canonical(spec, canonical)


def _decode_canonical(spec: TypeSpec, canonical: CanonicalValue) -> object:
    name = spec.name
    if name is AttributeType.INTEGER:
        assert isinstance(canonical, str)
        return int(canonical)
    if name is AttributeType.DECIMAL:
        assert isinstance(canonical, str)
        return Decimal(canonical)
    if name is AttributeType.FLOAT:
        assert isinstance(canonical, int | float)
        return float(canonical)
    if name is AttributeType.DATE:
        assert isinstance(canonical, str)
        return date.fromisoformat(canonical)
    if name is AttributeType.TIMESTAMP:
        assert isinstance(canonical, str)
        return _parse_timestamp(spec, canonical).astimezone(UTC)
    if name is AttributeType.DURATION:
        assert isinstance(canonical, str)
        return _parse_duration(spec, canonical)
    if name is AttributeType.GEO_POINT:
        assert isinstance(canonical, dict)
        lat, lon = canonical["lat"], canonical["lon"]
        assert isinstance(lat, int | float)
        assert isinstance(lon, int | float)
        return GeoPoint(float(lat), float(lon))
    if name is AttributeType.ARRAY:
        assert isinstance(canonical, list)
        return [_decode_canonical(spec.scalar, item) for item in canonical]
    return canonical


def float_text(value: float) -> str:
    """Shortest round-trip text of a float (``repr``), with negative zero as ``0.0``."""
    return repr(0.0 if value == 0.0 else value)


def to_text(spec: TypeSpec, value: CanonicalValue) -> str:
    """Text form of a canonical scalar value, used by casts to text types and by key rendering.

    Raises ``TypeSystemError`` for ``json`` and ``array`` values, which have no text form.
    """
    name = spec.name
    if name in (AttributeType.JSON, AttributeType.ARRAY):
        raise TypeSystemError(f"{spec.describe()} values have no text form")
    if name is AttributeType.BOOLEAN:
        return "true" if value else "false"
    if name is AttributeType.FLOAT:
        assert isinstance(value, int | float)
        return float_text(float(value))
    if name is AttributeType.GEO_POINT:
        assert isinstance(value, dict)
        lat, lon = value["lat"], value["lon"]
        assert isinstance(lat, int | float)
        assert isinstance(lon, int | float)
        return f"{float_text(float(lat))},{float_text(float(lon))}"
    assert isinstance(value, str)
    return value
