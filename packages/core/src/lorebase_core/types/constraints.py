"""Attribute constraints (design 5.4): ``min``, ``max``, ``min_length``, ``max_length``,
``pattern`` and ``enum_values``.

Applicability:

- ``min`` and ``max`` apply to ordered types (integer, decimal, float, date, timestamp,
  duration); bounds are inclusive and written as values of the Attribute type.
- ``min_length`` and ``max_length`` count Unicode code points of text values; on arrays they
  bound the number of elements.
- ``pattern`` applies to text values. It uses the RE2 subset (no lookaround, no
  backreferences) and matches anywhere in the value unless anchored with ``^`` and ``$``.
- ``enum_values`` restricts text, integer and decimal values to a closed list (the ``enum``
  type carries its own values instead).

On arrays, every constraint other than the length bounds applies to each element.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from lorebase_core.types.encoding import decode_value, encode_value
from lorebase_core.types.errors import EncodingError
from lorebase_core.types.types import (
    ORDERED_TYPES,
    TEXTUAL_TYPES,
    AttributeType,
    CanonicalValue,
    TypeSpec,
)

#: Types accepting ``enum_values`` as a constraint.
ENUM_CONSTRAINT_TYPES = TEXTUAL_TYPES | {AttributeType.INTEGER, AttributeType.DECIMAL}


class ConstraintName(StrEnum):
    MIN = "min"
    MAX = "max"
    MIN_LENGTH = "min_length"
    MAX_LENGTH = "max_length"
    PATTERN = "pattern"
    ENUM_VALUES = "enum_values"


class Constraints(BaseModel):
    """Value constraints of an Attribute. Unset constraints are ``None``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min: JsonValue = None
    max: JsonValue = None
    min_length: int | None = Field(default=None, ge=0)
    max_length: int | None = Field(default=None, ge=0)
    pattern: str | None = None
    enum_values: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def _check_lengths(self) -> Self:
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("min_length must not exceed max_length")
        return self

    @property
    def is_empty(self) -> bool:
        return all(getattr(self, name.value) is None for name in ConstraintName)


@dataclass(frozen=True, slots=True)
class ConstraintViolation:
    """A value that breaks one constraint. ``index`` locates the array element, if any."""

    constraint: ConstraintName
    message: str
    index: int | None = None


# RE2 compatibility -------------------------------------------------------------

_UNSUPPORTED_GROUPS = ("(?=", "(?!", "(?<=", "(?<!", "(?>", "(?(", "(?P=")


def re2_incompatibility(pattern: str) -> str | None:
    """Return why ``pattern`` uses syntax outside RE2, or ``None`` when it does not."""
    in_class = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            following = pattern[index + 1 : index + 2]
            if following and following in "123456789" and not in_class:
                return "backreferences are not supported"
            index += 2
            continue
        if in_class:
            if char == "]":
                in_class = False
        elif char == "[":
            in_class = True
            if pattern[index + 1 : index + 2] == "]":
                index += 1
        elif char == "(":
            for construct in _UNSUPPORTED_GROUPS:
                if pattern.startswith(construct, index):
                    return f"{construct} groups are not supported"
        index += 1
    return None


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a constraint pattern, rejecting syntax outside RE2 with ``ValueError``."""
    problem = re2_incompatibility(pattern)
    if problem is not None:
        raise ValueError(problem)
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid pattern: {exc}") from exc


# Static checks ----------------------------------------------------------------


def _bound(scalar: TypeSpec, raw: JsonValue) -> Any:
    return decode_value(scalar, encode_value(scalar, raw))


def constraint_problems(
    spec: TypeSpec, constraints: Constraints
) -> list[tuple[ConstraintName, str]]:
    """Static problems of ``constraints`` for an Attribute of type ``spec``."""
    problems: list[tuple[ConstraintName, str]] = []
    scalar = spec.scalar
    bounds: dict[ConstraintName, Any] = {}
    for name, raw in ((ConstraintName.MIN, constraints.min), (ConstraintName.MAX, constraints.max)):
        if raw is None:
            continue
        if scalar.name not in ORDERED_TYPES:
            problems.append((name, f"{name} does not apply to {spec.describe()}"))
            continue
        try:
            bounds[name] = _bound(scalar, raw)
        except EncodingError as exc:
            problems.append((name, f"{name} is not a valid {scalar.describe()}: {exc.message}"))
    if len(bounds) == 2 and bounds[ConstraintName.MIN] > bounds[ConstraintName.MAX]:
        problems.append((ConstraintName.MIN, "min must not exceed max"))

    length_applies = spec.is_array or scalar.name in TEXTUAL_TYPES
    for name, length in (
        (ConstraintName.MIN_LENGTH, constraints.min_length),
        (ConstraintName.MAX_LENGTH, constraints.max_length),
    ):
        if length is not None and not length_applies:
            problems.append((name, f"{name} does not apply to {spec.describe()}"))

    if constraints.pattern is not None:
        if scalar.name not in TEXTUAL_TYPES:
            problems.append(
                (ConstraintName.PATTERN, f"pattern does not apply to {spec.describe()}")
            )
        else:
            try:
                compile_pattern(constraints.pattern)
            except ValueError as exc:
                problems.append((ConstraintName.PATTERN, str(exc)))

    if constraints.enum_values is not None:
        if scalar.name not in ENUM_CONSTRAINT_TYPES:
            problems.append(
                (ConstraintName.ENUM_VALUES, f"enum_values does not apply to {spec.describe()}")
            )
        elif not constraints.enum_values:
            problems.append((ConstraintName.ENUM_VALUES, "enum_values must not be empty"))
        else:
            for raw_value in constraints.enum_values:
                try:
                    encode_value(scalar, raw_value)
                except EncodingError as exc:
                    problems.append(
                        (
                            ConstraintName.ENUM_VALUES,
                            f"{raw_value!r} is not a valid {scalar.describe()}: {exc.message}",
                        )
                    )
    return problems


# Value checks -----------------------------------------------------------------


def _check_scalar(
    scalar: TypeSpec,
    constraints: Constraints,
    value: CanonicalValue,
    index: int | None,
    *,
    check_length: bool,
) -> list[ConstraintViolation]:
    violations: list[ConstraintViolation] = []
    if scalar.name in ORDERED_TYPES and (
        constraints.min is not None or constraints.max is not None
    ):
        decoded = decode_value(scalar, value)
        if constraints.min is not None and decoded < _bound(scalar, constraints.min):
            violations.append(
                ConstraintViolation(
                    ConstraintName.MIN, f"{value} is below min {constraints.min}", index
                )
            )
        if constraints.max is not None and decoded > _bound(scalar, constraints.max):
            violations.append(
                ConstraintViolation(
                    ConstraintName.MAX, f"{value} is above max {constraints.max}", index
                )
            )
    if isinstance(value, str) and scalar.name in TEXTUAL_TYPES:
        if (
            check_length
            and constraints.min_length is not None
            and len(value) < constraints.min_length
        ):
            violations.append(
                ConstraintViolation(
                    ConstraintName.MIN_LENGTH,
                    f"length {len(value)} is below min_length {constraints.min_length}",
                    index,
                )
            )
        if (
            check_length
            and constraints.max_length is not None
            and len(value) > constraints.max_length
        ):
            violations.append(
                ConstraintViolation(
                    ConstraintName.MAX_LENGTH,
                    f"length {len(value)} is above max_length {constraints.max_length}",
                    index,
                )
            )
        if (
            constraints.pattern is not None
            and compile_pattern(constraints.pattern).search(value) is None
        ):
            violations.append(
                ConstraintViolation(
                    ConstraintName.PATTERN,
                    f"{value!r} does not match {constraints.pattern!r}",
                    index,
                )
            )
    if constraints.enum_values is not None and scalar.name in ENUM_CONSTRAINT_TYPES:
        allowed = {encode_value(scalar, raw) for raw in constraints.enum_values}
        if value not in allowed:
            violations.append(
                ConstraintViolation(
                    ConstraintName.ENUM_VALUES,
                    f"{value!r} is not one of {list(constraints.enum_values)}",
                    index,
                )
            )
    return violations


def check_constraints(
    spec: TypeSpec, constraints: Constraints, value: CanonicalValue
) -> list[ConstraintViolation]:
    """Constraint violations of a canonical ``value`` of type ``spec`` (absent values pass)."""
    if value is None or constraints.is_empty:
        return []
    if not spec.is_array:
        return _check_scalar(spec, constraints, value, None, check_length=True)
    assert isinstance(value, list)
    violations: list[ConstraintViolation] = []
    if constraints.min_length is not None and len(value) < constraints.min_length:
        violations.append(
            ConstraintViolation(
                ConstraintName.MIN_LENGTH,
                f"{len(value)} elements, below min_length {constraints.min_length}",
            )
        )
    if constraints.max_length is not None and len(value) > constraints.max_length:
        violations.append(
            ConstraintViolation(
                ConstraintName.MAX_LENGTH,
                f"{len(value)} elements, above max_length {constraints.max_length}",
            )
        )
    for index, item in enumerate(value):
        violations.extend(_check_scalar(spec.scalar, constraints, item, index, check_length=False))
    return violations
