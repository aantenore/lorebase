"""Column statistics and type detection.

Each non-null value is tested against the candidate types in precedence order: ``boolean``,
``integer``, ``decimal``, ``float``, ``date``, ``timestamp``, ``json``. The first type that at
least ``1 - type_tolerance`` of the values satisfy wins. Otherwise the column is textual:
``enum`` when it has few distinct short values, ``text`` when values are long or prose under a
text-like name, ``string`` (with ``max_length`` rounded up to a bucket and, for code-like
values, a ``pattern``) otherwise. A declared catalog type always wins over detection.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from itertools import islice

from lorebase_core.infer.names import matches, normalize
from lorebase_core.infer.options import InferenceOptions
from lorebase_core.infer.profile import ColumnProfile
from lorebase_core.types import (
    INT64_MAX,
    INT64_MIN,
    AttributeType,
    Constraints,
    EncodingError,
    TypeSpec,
    encode_value,
)

_INTEGER_TEXT = re.compile(r"[+-]?[0-9]+")
_DECIMAL_TEXT = re.compile(r"[+-]?([0-9]+)(?:\.([0-9]+))?")
_FLOAT_TEXT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", re.IGNORECASE)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_REGEX_SPECIALS = frozenset(".^$*+?()[]{}|\\/")
_TIMESTAMP = TypeSpec.of(AttributeType.TIMESTAMP)

#: Textual types: what a column is when no other type fits.
TEXT_FAMILY = frozenset({AttributeType.STRING, AttributeType.TEXT, AttributeType.ENUM})


class Family(StrEnum):
    """Value families that can reference each other (foreign key to key)."""

    INTEGER = "integer"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class MixedTypes:
    """A column whose values mostly, not all, parse as ``dominant``."""

    dominant: AttributeType
    share: float
    outliers: tuple[str, ...]
    accepted: bool


@dataclass(frozen=True)
class ColumnAnalysis:
    """What inference knows about one column."""

    name: str
    snake: str
    index: int
    count: int
    nulls: int
    #: Normalized text of each value read, ``None`` for nulls (row aligned).
    row_keys: tuple[str | None, ...]
    distinct_keys: frozenset[str]
    type: TypeSpec
    constraints: Constraints
    required: bool
    unique: bool
    declared_unique: bool
    declared_type: bool
    confidence: float
    reasons: tuple[str, ...]
    samples: tuple[str, ...]
    generated_ids: bool
    mixed: MixedTypes | None = None
    comment: str | None = None
    numeric_min: float | None = field(default=None)

    @property
    def non_null(self) -> int:
        return self.count - self.nulls

    @property
    def family(self) -> Family | None:
        name = self.type.name
        if name is AttributeType.INTEGER:
            return Family.INTEGER
        if name in (AttributeType.STRING, AttributeType.ENUM, AttributeType.URI):
            return Family.TEXT
        return None


def support(non_null: int, options: InferenceOptions) -> float:
    """Confidence factor for the amount of evidence: 0.5 with no values, 1 at
    ``min_support_values`` and above."""
    return 0.5 + 0.5 * min(1.0, non_null / options.min_support_values)


# Value readers -------------------------------------------------------------------------------


def text_of(value: object, options: InferenceOptions) -> str | None:
    """Text form of a value, or ``None`` for nulls and null tokens."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip() if options.trim_text else value
        return None if text in options.null_tokens else text
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return None if math.isnan(value) else repr(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def _leading_zero(digits: str, options: InferenceOptions) -> bool:
    return options.leading_zero_integers_as_text and len(digits) > 1 and digits[0] == "0"


def _as_bool(value: object, text: str, options: InferenceOptions) -> bool:
    if isinstance(value, bool):
        return True
    if not isinstance(value, str):
        return False
    lowered = text.lower()
    return lowered in {t.lower() for t in options.true_tokens} or lowered in {
        t.lower() for t in options.false_tokens
    }


def _as_int(value: object, text: str, options: InferenceOptions) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            return None
        number = int(value)
    elif isinstance(value, str):
        if not _INTEGER_TEXT.fullmatch(text) or _leading_zero(text.lstrip("+-"), options):
            return None
        number = int(text)
    else:
        return None
    return number if INT64_MIN <= number <= INT64_MAX else None


def _as_decimal(value: object, text: str, options: InferenceOptions) -> tuple[int, int] | None:
    """``(integer digits, scale)`` of an exact decimal number."""
    if isinstance(value, bool | float):
        return None
    if isinstance(value, int):
        return (len(str(abs(value))), 0)
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        exponent = value.as_tuple().exponent
        assert isinstance(exponent, int)
        scale = max(0, -exponent)
        integer_digits = len(format(abs(value).to_integral_value(rounding="ROUND_DOWN"), "f"))
        return (integer_digits, scale)
    if not isinstance(value, str):
        return None
    match = _DECIMAL_TEXT.fullmatch(text)
    if match is None or _leading_zero(match[1], options):
        return None
    return (len(match[1].lstrip("0") or "0"), len(match[2] or ""))


def _as_float(value: object, text: str, options: InferenceOptions) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float | Decimal):
        number = float(value)
    elif isinstance(value, str):
        if not _FLOAT_TEXT.fullmatch(text):
            return None
        integer_part = text.lstrip("+-").split(".")[0].split("e")[0].split("E")[0]
        if _leading_zero(integer_part, options):
            return None
        number = float(text)
    else:
        return None
    return number if math.isfinite(number) else None


def _as_date(value: object, text: str, options: InferenceOptions) -> bool:
    if isinstance(value, datetime):
        return False
    if isinstance(value, date):
        return True
    if not isinstance(value, str):
        return False
    for fmt in options.date_formats:
        try:
            datetime.strptime(text, fmt)
        except ValueError:
            continue
        return True
    return False


def _as_timestamp(value: object, text: str, options: InferenceOptions) -> tuple[bool, bool]:
    """``(is a timestamp, has no offset)``."""
    if isinstance(value, datetime):
        return (True, value.tzinfo is None)
    if not isinstance(value, str):
        return (False, False)
    try:
        encode_value(_TIMESTAMP, text)
    except EncodingError:
        pass
    else:
        return (True, False)
    for fmt in options.timestamp_formats:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return (True, parsed.tzinfo is None)
    return (False, False)


def _as_json(value: object, text: str) -> bool:
    if isinstance(value, dict | list):
        return True
    if not isinstance(value, str) or text[:1] not in ("{", "["):
        return False
    try:
        return isinstance(json.loads(text), dict | list)
    except ValueError:
        return False


# Text shapes ---------------------------------------------------------------------------------


def _runs(text: str) -> list[tuple[str, str]]:
    """``S-1001`` -> ``[("A", "S"), ("-", "-"), ("9", "1001")]``."""
    runs: list[tuple[str, str]] = []
    for char in text:
        if "A" <= char <= "Z":
            cls = "A"
        elif "a" <= char <= "z":
            cls = "a"
        elif "0" <= char <= "9":
            cls = "9"
        else:
            cls = char
        if runs and runs[-1][0] == cls and cls in ("A", "a", "9"):
            runs[-1] = (cls, runs[-1][1] + char)
        else:
            runs.append((cls, char))
    return runs


def _escape(text: str) -> str:
    return "".join("\\" + c if c in _REGEX_SPECIALS else c for c in text)


def shape_pattern(values: Sequence[str]) -> str | None:
    """A pattern shared by code-like ``values`` (``^S-[0-9]{4,}$``, ``^[A-Z]{2}$``), or
    ``None`` when values differ in shape or are not code-like (no digits and not a short
    upper-case code)."""
    shapes = [_runs(v) for v in values]
    classes = [cls for cls, _ in shapes[0]]
    if not classes or any([cls for cls, _ in shape] != classes for shape in shapes[1:]):
        return None
    code_like = "9" in classes or (classes == ["A"] and max(len(v) for v in values) <= 4)
    if not code_like or " " in classes:
        return None
    parts = []
    for position, cls in enumerate(classes):
        texts = [shape[position][1] for shape in shapes]
        lengths = [len(t) for t in texts]
        if cls not in ("A", "a", "9"):
            parts.append(_escape(cls))
        elif cls == "9":
            parts.append(f"[0-9]{{{min(lengths)},}}")
        elif len(set(texts)) == 1:
            parts.append(_escape(texts[0]))
        else:
            letters = "[A-Z]" if cls == "A" else "[a-z]"
            low, high = min(lengths), max(lengths)
            parts.append(f"{letters}{{{low}}}" if low == high else f"{letters}{{{low},{high}}}")
    return "^" + "".join(parts) + "$"


def generated_ids(texts: Iterable[str]) -> bool:
    """Whether every value is a ULID or a UUID."""
    seen = False
    for text in texts:
        if not (_ULID.fullmatch(text) or _UUID.fullmatch(text)):
            return False
        seen = True
    return seen


# Analysis ------------------------------------------------------------------------------------


@dataclass
class _Tally:
    """Per-type counts over the non-null values of a column."""

    boolean: int = 0
    integer: int = 0
    decimal: int = 0
    floating: int = 0
    date: int = 0
    timestamp: int = 0
    naive_timestamps: int = 0
    json: int = 0
    integer_digits: int = 1
    scale: int = 0
    negative: bool = False
    numeric_min: float | None = None
    failures: dict[AttributeType, list[str]] = field(default_factory=dict)

    def fail(self, name: AttributeType, text: str) -> None:
        self.failures.setdefault(name, []).append(text)


def _tally(values: Sequence[tuple[object, str]], options: InferenceOptions) -> _Tally:
    tally = _Tally()
    for value, text in values:
        if _as_bool(value, text, options):
            tally.boolean += 1
        else:
            tally.fail(AttributeType.BOOLEAN, text)
        if _as_int(value, text, options) is not None:
            tally.integer += 1
        else:
            tally.fail(AttributeType.INTEGER, text)
        digits = _as_decimal(value, text, options)
        if digits is not None:
            tally.decimal += 1
            tally.integer_digits = max(tally.integer_digits, digits[0])
            tally.scale = max(tally.scale, digits[1])
        else:
            tally.fail(AttributeType.DECIMAL, text)
        number = _as_float(value, text, options)
        if number is not None:
            tally.floating += 1
            tally.numeric_min = (
                number if tally.numeric_min is None else min(tally.numeric_min, number)
            )
        else:
            tally.fail(AttributeType.FLOAT, text)
        if _as_date(value, text, options):
            tally.date += 1
        else:
            tally.fail(AttributeType.DATE, text)
        is_timestamp, naive = _as_timestamp(value, text, options)
        if is_timestamp:
            tally.timestamp += 1
            tally.naive_timestamps += naive
        else:
            tally.fail(AttributeType.TIMESTAMP, text)
        if _as_json(value, text):
            tally.json += 1
        else:
            tally.fail(AttributeType.JSON, text)
    return tally


def _numbered(distinct: Sequence[str]) -> bool:
    """Whether the values are numbered identifiers of one shape (``analyst-1``, ``analyst-2``):
    such columns are open-ended codes, not enums."""
    if len(distinct) < 2:
        return False
    pattern = shape_pattern(distinct)
    return pattern is not None and "[0-9]" in pattern


def _bucket(length: int, options: InferenceOptions) -> int | None:
    return next((b for b in options.string_length_buckets if b >= length), None)


def _textual(
    snake: str, texts: Sequence[str], distinct: Sequence[str], options: InferenceOptions
) -> tuple[TypeSpec, Constraints, list[str]]:
    """Type, constraints and reasons of a column with no non-text type."""
    if not texts:
        return TypeSpec.of(AttributeType.STRING), Constraints(), ["no values: string by default"]
    longest = max(len(t) for t in texts)
    n = len(texts)
    ratio = len(distinct) / n
    if (
        n >= options.enum_min_values
        and len(distinct) <= options.enum_max_cardinality
        and ratio <= options.enum_max_distinct_ratio
        and longest <= options.enum_max_value_length
        and not any("\n" in t for t in distinct)
        and not _numbered(distinct)
    ):
        return (
            TypeSpec.enum(*sorted(distinct)),
            Constraints(),
            [f"{len(distinct)} distinct values in {n} (ratio {ratio:.2f}): enum"],
        )
    if longest > options.text_min_length:
        return (
            TypeSpec.of(AttributeType.TEXT),
            Constraints(),
            [f"values up to {longest} characters: text"],
        )
    words = sum(len(t.split()) for t in texts) / n
    if matches(snake, options.text_name_hints) and words >= options.text_hint_min_words:
        return (
            TypeSpec.of(AttributeType.TEXT),
            Constraints(),
            [f"prose ({words:.1f} words on average) under a text-like name: text"],
        )
    reasons = [f"text values up to {longest} characters: string"]
    max_length = _bucket(longest, options) if options.propose_max_length else None
    pattern = None
    if options.propose_patterns and len(distinct) >= options.pattern_min_values:
        pattern = shape_pattern(distinct)
        if pattern is not None:
            reasons.append(f"every value has the shape {pattern}")
    if max_length is not None:
        reasons.append(f"max_length {max_length} (longest value rounded up)")
    return (
        TypeSpec.of(AttributeType.STRING),
        Constraints(max_length=max_length, pattern=pattern),
        reasons,
    )


def _detect(
    snake: str,
    pairs: Sequence[tuple[object, str]],
    distinct: Sequence[str],
    options: InferenceOptions,
) -> tuple[TypeSpec, Constraints, float, list[str], MixedTypes | None, float | None]:
    """Type, constraints, share of conforming values, reasons, mixed-type report, minimum."""
    n = len(pairs)
    texts = [text for _, text in pairs]
    if n == 0:
        spec, constraints, reasons = _textual(snake, texts, distinct, options)
        return spec, constraints, 1.0, reasons, None, None
    tally = _tally(pairs, options)
    needed = math.ceil(n * (1 - options.type_tolerance))
    numeric_min = tally.numeric_min
    non_negative = options.propose_non_negative_min and numeric_min is not None and numeric_min >= 0
    numeric_constraints = Constraints(min=0) if non_negative else Constraints()
    numeric_reason = ["never negative: min 0"] if non_negative else []

    candidates: list[tuple[AttributeType, int]] = [
        (AttributeType.BOOLEAN, tally.boolean),
        (AttributeType.INTEGER, tally.integer),
        (
            AttributeType.DECIMAL,
            tally.decimal if 0 < tally.scale <= options.decimal_max_scale else 0,
        ),
        (AttributeType.FLOAT, tally.floating),
        (AttributeType.DATE, tally.date),
        (AttributeType.TIMESTAMP, tally.timestamp),
        (AttributeType.JSON, tally.json),
    ]
    for name, count in candidates:
        if count < needed or count == 0:
            continue
        share = count / n
        mixed = None
        if count < n:
            outliers = tuple(dict.fromkeys(tally.failures.get(name, ())))[
                : options.max_evidence_samples
            ]
            mixed = MixedTypes(name, share, outliers, accepted=True)
        reasons = [f"{count} of {n} values are {name}"]
        if name is AttributeType.DECIMAL:
            precision = min(
                options.decimal_max_precision,
                tally.integer_digits + options.decimal_precision_headroom + tally.scale,
            )
            scale = min(tally.scale, precision)
            reasons.append(
                f"up to {tally.integer_digits} integer and {tally.scale} fractional digits"
            )
            return (
                TypeSpec.decimal(precision, scale),
                numeric_constraints,
                share,
                reasons + numeric_reason,
                mixed,
                numeric_min,
            )
        if name is AttributeType.FLOAT and tally.scale > options.decimal_max_scale:
            reasons.append(f"{tally.scale} fractional digits exceed decimal_max_scale")
        if name in (AttributeType.INTEGER, AttributeType.FLOAT):
            return (
                TypeSpec.of(name),
                numeric_constraints,
                share,
                reasons + numeric_reason,
                mixed,
                numeric_min,
            )
        if name is AttributeType.TIMESTAMP and tally.naive_timestamps:
            reasons.append("values without offset are read as UTC")
        return TypeSpec.of(name), Constraints(), share, reasons, mixed, numeric_min

    spec, constraints, reasons = _textual(snake, texts, distinct, options)
    mixed = None
    best_name, best_count = max(
        ((name, count) for name, count in candidates if name is not AttributeType.JSON),
        key=lambda item: item[1],
    )
    if best_count and best_count / n >= options.mixed_type_report_ratio:
        outliers = tuple(dict.fromkeys(tally.failures.get(best_name, ())))[
            : options.max_evidence_samples
        ]
        mixed = MixedTypes(best_name, best_count / n, outliers, accepted=False)
        reasons.append(f"{best_count} of {n} values are {best_name}; the others are not")
    return spec, constraints, 1.0, reasons, mixed, None


def _key_text(spec: TypeSpec, value: object, text: str) -> str:
    """Comparable form of a value for uniqueness and references (``007`` stays ``007``)."""
    if spec.name is AttributeType.INTEGER:
        try:
            return str(int(Decimal(text)))
        except (InvalidOperation, ValueError):
            return text
    return text


def analyze_column(profile: ColumnProfile, index: int, options: InferenceOptions) -> ColumnAnalysis:
    """Statistics, type, nullability and uniqueness of one column."""
    raw = list(islice(profile.values, options.sample_size))
    texts = [text_of(value, options) for value in raw]
    pairs = [(value, text) for value, text in zip(raw, texts, strict=True) if text is not None]
    nulls = len(raw) - len(pairs)
    distinct = list(dict.fromkeys(text for _, text in pairs))
    snake = normalize(profile.name)

    if profile.declared_type is not None:
        spec = profile.declared_type
        max_length = profile.declared_max_length
        constraints = (
            Constraints(max_length=max_length) if max_length is not None else Constraints()
        )
        share, mixed, numeric_min = 1.0, None, None
        reasons = [f"declared type {spec.describe()}"]
        if max_length is not None:
            reasons.append(f"declared max_length {max_length}")
    else:
        spec, constraints, share, reasons, mixed, numeric_min = _detect(
            snake, pairs, distinct, options
        )

    row_keys = tuple(
        None if text is None else _key_text(spec, value, text)
        for value, text in zip(raw, texts, strict=True)
    )
    keys = [k for k in row_keys if k is not None]
    distinct_keys = frozenset(keys)
    non_null = len(keys)

    if profile.nullable is not None:
        required = not profile.nullable
        reasons.append("declared NOT NULL" if required else "declared nullable")
    else:
        required = bool(raw) and nulls / len(raw) <= options.required_max_null_ratio
        reasons.append(
            f"no nulls in {len(raw)} values: required"
            if required and not nulls
            else f"{nulls} nulls in {len(raw)} values"
            + (": required" if required else ": optional")
        )
    observed_unique = non_null >= options.unique_min_values and len(distinct_keys) == non_null
    unique = bool(profile.unique) or observed_unique
    if profile.unique:
        reasons.append("declared unique")
    elif observed_unique:
        reasons.append(f"{non_null} distinct values in {non_null}: unique")

    confidence = 1.0 if profile.declared_type is not None else share * support(non_null, options)
    return ColumnAnalysis(
        name=profile.name,
        snake=snake,
        index=index,
        count=len(raw),
        nulls=nulls,
        row_keys=row_keys,
        distinct_keys=distinct_keys,
        type=spec,
        constraints=constraints,
        required=required,
        unique=unique,
        declared_unique=bool(profile.unique),
        declared_type=profile.declared_type is not None,
        confidence=round(confidence, 4),
        reasons=tuple(reasons),
        samples=tuple(distinct[: options.max_evidence_samples]),
        generated_ids=generated_ids(distinct),
        mixed=mixed,
        comment=profile.comment,
        numeric_min=numeric_min,
    )
