"""Normalization stage of reconciliation (design 10.6, stage c).

Competing ``string`` and ``text`` values are compared in a looser form than their canonical
one, to drop candidates that only differ in spelling and to rank the others. Nothing here
changes a stored value.

- **Quantities.** A number (``.`` as decimal point, no grouping separators) followed by a unit
  of the units table (``5 km``, ``5000 m``) reads as a magnitude in the dimension's base unit;
  quantities of one dimension are equal when their magnitudes are, those of different
  dimensions differ. The table ships with ``lorebase_core``
  (``units.yaml``); ``normalize.units_file`` replaces it.
- **Numbers.** Plain numbers, and magnitudes, are equal within ``normalize.numeric_tolerance``
  (relative to the larger one; 0 compares exactly).
- **Text.** Values that differ only in case, spacing, compatibility forms or punctuation are
  equal.
- **Negation.** Values equal once the words of ``normalize.negation_words`` are removed are not
  equal: they are kept and marked ``negation``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import JsonValue

from lorebase_core.reconcile.candidates import Signal
from lorebase_core.reconcile.claims import Claim, value_hash
from lorebase_core.reconcile.options import NormalizeOptions
from lorebase_core.types import fold_text

DEFAULT_UNITS = "units.yaml"


class Comparison(StrEnum):
    EQUAL = "equal"
    DIFFERENT = "different"
    NEGATION = "negation"


class UnitTableError(ValueError):
    """A units file that cannot be read."""


@dataclass(frozen=True, slots=True)
class Unit:
    symbol: str
    dimension: str
    factor: Decimal


@dataclass(frozen=True, slots=True)
class Quantity:
    """A magnitude in the base unit of ``dimension`` (``None`` for a plain number)."""

    magnitude: Decimal
    dimension: str | None
    unit: str | None = None


class UnitTable:
    """Units by spelling: exact first, then case-folded when only one unit answers to it."""

    def __init__(self, units: Iterable[tuple[str, Unit]]) -> None:
        self._exact: dict[str, Unit] = {}
        folded: dict[str, set[Unit]] = {}
        for spelling, unit in units:
            self._exact[spelling] = unit
            folded.setdefault(fold_text(spelling), set()).add(unit)
        self._folded = {k: next(iter(v)) for k, v in folded.items() if len(v) == 1}

    def get(self, spelling: str) -> Unit | None:
        found = self._exact.get(spelling)
        return found if found is not None else self._folded.get(fold_text(spelling))

    @classmethod
    def parse(cls, document: Any) -> UnitTable:
        """A table from the YAML document: ``{dimension: {symbol: {factor, aliases}}}``."""
        if not isinstance(document, Mapping):
            raise UnitTableError("a units table maps dimensions to units")
        entries: list[tuple[str, Unit]] = []
        for dimension, units in document.items():
            if not isinstance(units, Mapping):
                raise UnitTableError(f"dimension {dimension!r}: expected a mapping of units")
            for symbol, spec in units.items():
                if not isinstance(spec, Mapping) or "factor" not in spec:
                    raise UnitTableError(f"unit {symbol!r}: expected a mapping with a factor")
                try:
                    factor = Decimal(str(spec["factor"]))
                except InvalidOperation:
                    raise UnitTableError(f"unit {symbol!r}: factor is not a number") from None
                if not factor.is_finite() or factor <= 0:
                    raise UnitTableError(f"unit {symbol!r}: factor must be positive")
                unit = Unit(str(symbol), str(dimension), factor)
                aliases = spec.get("aliases") or []
                if not isinstance(aliases, list):
                    raise UnitTableError(f"unit {symbol!r}: aliases must be a list")
                entries.extend((str(s), unit) for s in [symbol, *aliases])
        return cls(entries)

    @classmethod
    def load(cls, path: str | Path | None = None) -> UnitTable:
        """The table of ``path``, or the one shipped with ``lorebase_core``."""
        if path is None:
            return _default_table()
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as error:
            raise UnitTableError(f"cannot read units file {path}: {error}") from None
        return cls.parse(yaml.safe_load(text))


@cache
def _default_table() -> UnitTable:
    text = resources.files(__package__).joinpath(DEFAULT_UNITS).read_text(encoding="utf-8")
    return UnitTable.parse(yaml.safe_load(text))


_QUANTITY = re.compile(
    r"^\s*(?P<number>[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(?P<unit>[^\d\s].*?)?\s*$"
)
_PUNCTUATION = frozenset("PS")  # Unicode categories folded away: punctuation, symbols


def parse_quantity(text: str, units: UnitTable) -> Quantity | None:
    """``5 km`` -> 5000 in ``length``; ``42`` -> a plain number; ``None`` when the text is not a
    number, optionally followed by a unit of ``units``."""
    match = _QUANTITY.match(text)
    if match is None:
        return None
    try:
        number = Decimal(match["number"])
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    spelling = match["unit"]
    if spelling is None:
        return Quantity(number, None)
    unit = units.get(spelling.strip())
    if unit is None:
        return None
    return Quantity(number * unit.factor, unit.dimension, unit.symbol)


def numbers_equal(first: Decimal, second: Decimal, tolerance: float) -> bool:
    """Equal within ``tolerance`` relative to the larger magnitude."""
    if first == second:
        return True
    if tolerance <= 0:
        return False
    return abs(first - second) <= Decimal(str(tolerance)) * max(abs(first), abs(second))


def fold_words(text: str) -> tuple[str, ...]:
    """Words of ``text`` without case, compatibility forms, punctuation or symbols."""
    kept = "".join(
        " " if unicodedata.category(ch)[0] in _PUNCTUATION else ch
        for ch in fold_text(unicodedata.normalize("NFKC", text))
    )
    return tuple(kept.split())


def _without(words: Sequence[str], negations: frozenset[str]) -> tuple[str, ...]:
    return tuple(w for w in words if w not in negations)


def _as_number(value: JsonValue) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = Decimal(str(value))
    return number if number.is_finite() else None


def compare(
    first: JsonValue, second: JsonValue, *, options: NormalizeOptions, units: UnitTable
) -> Comparison:
    """How two canonical values compare once normalized (module doc)."""
    if value_hash(first) == value_hash(second):
        return Comparison.EQUAL
    a, b = _as_number(first), _as_number(second)
    if a is not None and b is not None:
        equal = numbers_equal(a, b, options.numeric_tolerance)
        return Comparison.EQUAL if equal else Comparison.DIFFERENT
    if not isinstance(first, str) or not isinstance(second, str):
        return Comparison.DIFFERENT
    qa, qb = parse_quantity(first, units), parse_quantity(second, units)
    if qa is not None and qb is not None:
        if qa.dimension != qb.dimension:
            return Comparison.DIFFERENT
        equal = numbers_equal(qa.magnitude, qb.magnitude, options.numeric_tolerance)
        return Comparison.EQUAL if equal else Comparison.DIFFERENT
    wa, wb = fold_words(first), fold_words(second)
    if wa == wb:
        return Comparison.EQUAL
    negations = frozenset(options.negation_words)
    if negations and _without(wa, negations) == _without(wb, negations):
        return Comparison.NEGATION
    return Comparison.DIFFERENT


@dataclass(frozen=True, slots=True)
class Normalized:
    """Claims grouped by normalized value: one group per value that still differs. ``signals``
    holds ``unit`` when quantities in different units were found equal and ``negation`` when
    two groups differ by a negation word."""

    groups: tuple[tuple[Claim, ...], ...]
    signals: frozenset[str]

    @property
    def differs(self) -> bool:
        return len(self.groups) > 1


def normalize_claims(
    claims: Iterable[Claim], *, options: NormalizeOptions, units: UnitTable
) -> Normalized:
    """Group ``claims`` whose values compare equal (``compare``), in order of appearance; each
    group is compared through its first claim."""
    groups: list[list[Claim]] = []
    signals: set[str] = set()
    for claim in claims:
        for group in groups:
            outcome = compare(group[0].value, claim.value, options=options, units=units)
            if outcome is Comparison.EQUAL:
                if group[0].value_hash != claim.value_hash and _units_differ(
                    group[0], claim, units
                ):
                    signals.add(Signal.UNIT.value)
                group.append(claim)
                break
        else:
            groups.append([claim])
    for i, first in enumerate(groups):
        for second in groups[i + 1 :]:
            outcome = compare(first[0].value, second[0].value, options=options, units=units)
            if outcome is Comparison.NEGATION:
                signals.add(Signal.NEGATION.value)
    return Normalized(tuple(tuple(g) for g in groups), frozenset(signals))


def _units_differ(first: Claim, second: Claim, units: UnitTable) -> bool:
    if not isinstance(first.value, str) or not isinstance(second.value, str):
        return False
    qa, qb = parse_quantity(first.value, units), parse_quantity(second.value, units)
    return qa is not None and qb is not None and qa.unit != qb.unit


__all__ = [
    "DEFAULT_UNITS",
    "Comparison",
    "Normalized",
    "Quantity",
    "Unit",
    "UnitTable",
    "UnitTableError",
    "compare",
    "fold_words",
    "normalize_claims",
    "numbers_equal",
    "parse_quantity",
]
