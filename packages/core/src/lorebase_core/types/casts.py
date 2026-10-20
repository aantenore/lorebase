"""Cast rules between Attribute types (design 5.4, "Type change rules").

Every pair of base types has a declared rule:

- ``safe``: always succeeds for every valid value of the source type;
- ``checked``: may fail per value (for example ``string`` to ``integer``);
- ``forbidden``: never allowed (for example ``geo_point`` to ``boolean``).

Arrays derive their rule from their element type: ``array<T> -> array<U>`` and
``T -> array<U>`` (wrap) take the rule of ``T -> U``; ``array<T> -> U`` (unwrap, at most one
element) is ``checked`` unless ``T -> U`` is forbidden.

Parameters can only make a rule stricter: a ``safe`` base rule becomes ``checked`` when the
target parameters are narrower (for example ``decimal(12,2) -> decimal(8,2)``, an ``enum``
losing values, or a ``ref`` to a different Kind).
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from lorebase_core.types.encoding import encode_value, float_text, to_text
from lorebase_core.types.errors import CastError, EncodingError, TypeSystemError
from lorebase_core.types.types import AttributeType, CanonicalValue, TypeSpec


class CastRule(StrEnum):
    SAFE = "safe"
    CHECKED = "checked"
    FORBIDDEN = "forbidden"


#: Marker in the matrix for pairs whose rule derives from the element rule (arrays).
ELEMENT_RULE = "element"

_T = AttributeType

# Rows: source type. Columns: target type. Abbreviations are listed in ``_ABBREVIATIONS``.
# S = safe, C = checked, F = forbidden, E = derived from the element rule (arrays).
_MATRIX_TABLE = """
       str  txt  uri  enm  int  dec  flt  bool date ts   dur  json geo  ref  arr
str    S    S    C    C    C    C    C    C    C    C    C    F    C    C    E
txt    S    S    C    C    C    C    C    C    C    C    C    F    C    C    E
uri    S    S    S    C    F    F    F    F    F    F    F    F    F    C    E
enm    S    S    C    S    C    C    C    C    C    C    C    F    F    C    E
int    S    S    F    C    S    S    C    C    F    F    F    F    F    C    E
dec    S    S    F    C    C    S    C    F    F    F    F    F    F    F    E
flt    S    S    F    C    C    C    S    F    F    F    F    F    F    F    E
bool   S    S    F    C    S    S    S    S    F    F    F    F    F    F    E
date   S    S    F    C    F    F    F    F    S    S    F    F    F    F    E
ts     S    S    F    C    F    F    F    F    C    S    F    F    F    F    E
dur    S    S    F    C    F    F    F    F    F    F    S    F    F    F    E
json   F    F    F    F    F    F    F    F    F    F    F    S    F    F    F
geo    S    S    F    F    F    F    F    F    F    F    F    F    S    F    E
ref    S    S    F    C    C    F    F    F    F    F    F    F    F    S    E
arr    F    F    E    E    E    E    E    E    E    E    E    F    E    E    E
"""

_ABBREVIATIONS: dict[str, AttributeType] = {
    "str": _T.STRING,
    "txt": _T.TEXT,
    "uri": _T.URI,
    "enm": _T.ENUM,
    "int": _T.INTEGER,
    "dec": _T.DECIMAL,
    "flt": _T.FLOAT,
    "bool": _T.BOOLEAN,
    "date": _T.DATE,
    "ts": _T.TIMESTAMP,
    "dur": _T.DURATION,
    "json": _T.JSON,
    "geo": _T.GEO_POINT,
    "ref": _T.REF,
    "arr": _T.ARRAY,
}

_CODES: dict[str, CastRule | str] = {
    "S": CastRule.SAFE,
    "C": CastRule.CHECKED,
    "F": CastRule.FORBIDDEN,
    "E": ELEMENT_RULE,
}


def _parse_matrix(table: str) -> dict[tuple[AttributeType, AttributeType], CastRule | str]:
    lines = [line.split() for line in table.strip().splitlines()]
    header = [_ABBREVIATIONS[name] for name in lines[0]]
    matrix: dict[tuple[AttributeType, AttributeType], CastRule | str] = {}
    for row in lines[1:]:
        source = _ABBREVIATIONS[row[0]]
        if len(row) - 1 != len(header):
            raise ValueError(f"cast matrix row {source} has {len(row) - 1} entries")
        for target, code in zip(header, row[1:], strict=True):
            matrix[(source, target)] = _CODES[code]
    return matrix


#: The declared base rule of every ``(from, to)`` pair of base types.
CAST_MATRIX = _parse_matrix(_MATRIX_TABLE)

#: Integer digits needed to hold any int64 value.
_INT64_DIGITS = 19


def _integer_digits(spec: TypeSpec) -> int:
    precision, scale = spec.precision, spec.scale
    assert precision is not None
    assert scale is not None
    return precision - scale


def _refine(source: TypeSpec, target: TypeSpec) -> CastRule:
    """Downgrade a ``safe`` base rule to ``checked`` when target parameters are narrower."""
    if target.name is _T.DECIMAL:
        if source.name is _T.DECIMAL:
            source_scale, target_scale = source.scale, target.scale
            assert source_scale is not None
            assert target_scale is not None
            widening = target_scale >= source_scale
            widening = widening and _integer_digits(target) >= _integer_digits(source)
            return CastRule.SAFE if widening else CastRule.CHECKED
        if source.name is _T.INTEGER:
            return CastRule.SAFE if _integer_digits(target) >= _INT64_DIGITS else CastRule.CHECKED
        if source.name is _T.BOOLEAN:
            return CastRule.SAFE if _integer_digits(target) >= 1 else CastRule.CHECKED
    if source.name is _T.ENUM and target.name is _T.ENUM:
        source_values, target_values = source.enum_values, target.enum_values
        assert source_values is not None
        assert target_values is not None
        return CastRule.SAFE if set(source_values) <= set(target_values) else CastRule.CHECKED
    if source.name is _T.REF and target.name is _T.REF:
        return CastRule.SAFE if source.kind_ref == target.kind_ref else CastRule.CHECKED
    return CastRule.SAFE


def cast_rule(source: TypeSpec, target: TypeSpec) -> CastRule:
    """The rule for casting values of ``source`` to ``target``, parameters included."""
    if source.element is not None and target.element is not None:
        return cast_rule(source.element, target.element)
    if source.element is not None:
        inner = cast_rule(source.element, target)
        return CastRule.FORBIDDEN if inner is CastRule.FORBIDDEN else CastRule.CHECKED
    if target.element is not None:
        if source.name is _T.JSON:
            return CastRule.FORBIDDEN
        return cast_rule(source, target.element)
    base = CAST_MATRIX[(source.name, target.name)]
    assert isinstance(base, CastRule)
    if base is not CastRule.SAFE:
        return base
    return _refine(source, target)


def cast(source: TypeSpec, target: TypeSpec, value: CanonicalValue) -> CanonicalValue:
    """Cast a canonical value of ``source`` into the canonical value of ``target``.

    ``None`` (absent) casts to ``None`` unless the rule is forbidden. Raises ``CastError`` when
    the rule is forbidden, when ``value`` is not a valid value of ``source``, or when a checked
    cast fails.
    """
    if cast_rule(source, target) is CastRule.FORBIDDEN:
        raise CastError(source.describe(), target.describe(), "the cast rule is forbidden")
    if value is None:
        return None
    try:
        canonical = encode_value(source, value)
    except EncodingError as exc:
        raise CastError(
            source.describe(), target.describe(), f"invalid source value: {exc.message}"
        ) from exc
    try:
        return _cast(source, target, canonical)
    except CastError:
        raise
    except TypeSystemError as exc:
        message = exc.message if isinstance(exc, EncodingError) else str(exc)
        raise CastError(source.describe(), target.describe(), message) from exc


def _fail(source: TypeSpec, target: TypeSpec, message: str) -> CastError:
    return CastError(source.describe(), target.describe(), message)


_TEXT_SOURCES = frozenset({_T.STRING, _T.TEXT, _T.URI, _T.ENUM, _T.REF})


def _cast(source: TypeSpec, target: TypeSpec, value: CanonicalValue) -> CanonicalValue:
    if value is None:
        return None
    if source.element is not None and target.element is not None:
        assert isinstance(value, list)
        return [_cast(source.element, target.element, item) for item in value]
    if source.element is not None:
        assert isinstance(value, list)
        if not value:
            return None
        if len(value) > 1:
            raise _fail(
                source, target, f"array has {len(value)} elements; at most one can be unwrapped"
            )
        return _cast(source.element, target, value[0])
    if target.element is not None:
        return [_cast(source, target.element, value)]

    if source == target:
        return value
    src, dst = source.name, target.name
    if dst in (_T.STRING, _T.TEXT):
        return encode_value(target, to_text(source, value))
    if src is dst:
        return encode_value(target, value)
    if dst in (_T.ENUM, _T.REF):
        return encode_value(target, to_text(source, value))
    if src in _TEXT_SOURCES:
        assert isinstance(value, str)
        return _cast_from_text(source, target, value)
    if src is _T.INTEGER:
        assert isinstance(value, str)
        if dst is _T.BOOLEAN:
            if value not in ("0", "1"):
                raise _fail(source, target, f"only 0 and 1 cast to boolean, got {value}")
            return value == "1"
        if dst is _T.FLOAT:
            return encode_value(target, int(value))
        return encode_value(target, value)
    if src is _T.DECIMAL:
        assert isinstance(value, str)
        number = Decimal(value)
        if dst is _T.FLOAT:
            as_float = float(number)
            if Decimal(float_text(as_float)) != number:
                raise _fail(source, target, f"{value} is not exactly representable as float")
            return encode_value(target, as_float)
        return encode_value(target, number)
    if src is _T.FLOAT:
        assert isinstance(value, int | float)
        number_f = float(value)
        if dst is _T.INTEGER:
            if not number_f.is_integer():
                raise _fail(source, target, f"{float_text(number_f)} is not integral")
            return encode_value(target, int(number_f))
        return encode_value(target, number_f)
    if src is _T.BOOLEAN:
        assert isinstance(value, bool)
        return encode_value(target, 1 if value else 0)
    if src is _T.DATE and dst is _T.TIMESTAMP:
        assert isinstance(value, str)
        return f"{value}T00:00:00.000000Z"
    if src is _T.TIMESTAMP and dst is _T.DATE:
        assert isinstance(value, str)
        day, time = value[:10], value[10:]
        if time != "T00:00:00.000000Z":
            raise _fail(source, target, f"{value} is not at midnight UTC")
        return day
    raise _fail(source, target, "no conversion defined")


def _cast_from_text(source: TypeSpec, target: TypeSpec, text: str) -> CanonicalValue:
    if target.name is _T.BOOLEAN:
        if text == "true":
            return True
        if text == "false":
            return False
        raise _fail(source, target, f"only 'true' and 'false' cast to boolean, got {text!r}")
    if target.name is _T.GEO_POINT:
        parts = [part.strip() for part in text.split(",")]
        if len(parts) != 2:
            raise _fail(source, target, f"expected 'lat,lon', got {text!r}")
        return encode_value(target, (parts[0], parts[1]))
    return encode_value(target, text)
