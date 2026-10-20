from __future__ import annotations

import pytest
from pydantic import ValidationError

from lorebase_core.types import (
    ConstraintName,
    Constraints,
    TypeSpec,
    check_constraints,
    compile_pattern,
    constraint_problems,
)

T = TypeSpec.of
C = ConstraintName


@pytest.mark.parametrize(
    ("spec", "constraints", "value", "expected"),
    [
        (T("integer"), Constraints(min=0, max=100), "50", []),
        (T("integer"), Constraints(min=0, max=100), "0", []),
        (T("integer"), Constraints(min=0, max=100), "-1", [C.MIN]),
        (T("integer"), Constraints(min=0, max=100), "101", [C.MAX]),
        (TypeSpec.decimal(5, 2), Constraints(min="0.50"), "0.49", [C.MIN]),
        (T("float"), Constraints(max=1.5), 1.6, [C.MAX]),
        (T("date"), Constraints(min="2026-01-01"), "2025-12-31", [C.MIN]),
        (
            T("timestamp"),
            Constraints(max="2026-01-01T00:00:00Z"),
            "2026-01-01T00:00:00.000001Z",
            [C.MAX],
        ),
        (T("duration"), Constraints(max="PT1H"), "P0DT1H0M0.000000S", []),
        (T("string"), Constraints(min_length=2, max_length=3), "ab", []),
        (T("string"), Constraints(min_length=2), "a", [C.MIN_LENGTH]),
        (T("string"), Constraints(max_length=3), "abcd", [C.MAX_LENGTH]),
        (T("string"), Constraints(max_length=4), "caf\u00e9", []),
        (T("string"), Constraints(pattern="^S-[0-9]+$"), "S-1001", []),
        (T("string"), Constraints(pattern="^S-[0-9]+$"), "X-1001", [C.PATTERN]),
        (T("string"), Constraints(pattern="[0-9]"), "abc1", []),
        (T("string"), Constraints(enum_values=("IT", "DE")), "FR", [C.ENUM_VALUES]),
        (T("integer"), Constraints(enum_values=("1", "2")), "2", []),
        (T("integer"), Constraints(enum_values=("1", "2")), "3", [C.ENUM_VALUES]),
        (TypeSpec.array(T("string")), Constraints(max_length=2), ["a", "b", "c"], [C.MAX_LENGTH]),
        (TypeSpec.array(T("string")), Constraints(min_length=1), [], [C.MIN_LENGTH]),
        (TypeSpec.array(T("string")), Constraints(max_length=1, pattern="^a"), ["abc"], []),
        (TypeSpec.array(T("integer")), Constraints(max=5), ["1", "9"], [C.MAX]),
        (T("string"), Constraints(max_length=1), None, []),
    ],
)
def test_check_constraints(
    spec: TypeSpec, constraints: Constraints, value: object, expected: list[C]
) -> None:
    violations = check_constraints(spec, constraints, value)  # type: ignore[arg-type]
    assert [v.constraint for v in violations] == expected


def test_array_violation_carries_index() -> None:
    violations = check_constraints(TypeSpec.array(T("integer")), Constraints(max=5), ["1", "9"])
    assert violations[0].index == 1


@pytest.mark.parametrize(
    ("spec", "constraints", "expected"),
    [
        (T("integer"), Constraints(min=0, max=10), []),
        (T("integer"), Constraints(min=10, max=0), [C.MIN]),
        (T("integer"), Constraints(min="x"), [C.MIN]),
        (T("string"), Constraints(min="a"), [C.MIN]),
        (T("boolean"), Constraints(max_length=3), [C.MAX_LENGTH]),
        (TypeSpec.array(T("integer")), Constraints(max_length=3), []),
        (T("integer"), Constraints(pattern="^1"), [C.PATTERN]),
        (T("string"), Constraints(pattern="("), [C.PATTERN]),
        (T("string"), Constraints(pattern="a(?=b)"), [C.PATTERN]),
        (T("string"), Constraints(pattern=r"(a)\1"), [C.PATTERN]),
        (T("string"), Constraints(pattern=r"[(?=]\\"), []),
        (T("boolean"), Constraints(enum_values=("true",)), [C.ENUM_VALUES]),
        (T("integer"), Constraints(enum_values=("x",)), [C.ENUM_VALUES]),
        (T("string"), Constraints(enum_values=()), [C.ENUM_VALUES]),
        (TypeSpec.enum("a"), Constraints(enum_values=("a",)), [C.ENUM_VALUES]),
    ],
)
def test_constraint_problems(spec: TypeSpec, constraints: Constraints, expected: list[C]) -> None:
    assert [name for name, _ in constraint_problems(spec, constraints)] == expected


def test_length_bounds_are_ordered() -> None:
    with pytest.raises(ValidationError, match="min_length must not exceed max_length"):
        Constraints(min_length=3, max_length=2)
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        Constraints(min_length=-1)


def test_compile_pattern_rejects_lookbehind() -> None:
    with pytest.raises(ValueError, match="not supported"):
        compile_pattern("(?<!a)b")
    assert compile_pattern(r"\(?=").search("(=") is not None
