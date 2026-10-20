"""Type detection, nullability, uniqueness and constraints of one column (table driven)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from lorebase_core.infer import ColumnProfile, InferenceOptions
from lorebase_core.infer.columns import ColumnAnalysis, analyze_column, shape_pattern
from lorebase_core.types import AttributeType, TypeSpec

OPTIONS = InferenceOptions()
COLORS = ["red", "green", "blue"] * 5
LONG = "word " * 60


def analyze(values: list[object], name: str = "col", **options: object) -> ColumnAnalysis:
    return analyze_column(ColumnProfile(name, values), 0, InferenceOptions.model_validate(options))


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["true", "false", "TRUE", "no", "Y"], "boolean"),
        ([True, False, True], "boolean"),
        (["1", "22", "-3", "+4"], "integer"),
        ([1, 2, 3], "integer"),
        (["0", "10", "200"], "integer"),
        (["1.5", "2.25", "3"], "decimal(5,2)"),
        (["922000.00", "10.5"], "decimal(10,2)"),
        ([Decimal("1.25"), Decimal("10.5")], "decimal(6,2)"),
        (["0.123456", "1.5"], "float"),
        (["1e3", "2.5E-2"], "float"),
        ([0.5, 1.25], "float"),
        (["2024-01-31", "2023-12-01"], "date"),
        ([date(2024, 1, 31)], "date"),
        (["2023-01-27T09:30:00.000000Z", "2024-05-01T10:00:00+02:00"], "timestamp"),
        (["2023-01-27 09:30:00", "2023-01-28 10:00:00"], "timestamp"),
        ([datetime(2024, 1, 1, tzinfo=UTC)], "timestamp"),
        (['{"a": 1}', "[1, 2]"], "json"),
        ([{"a": 1}, [1]], "json"),
        (["007", "012", "100"], "string"),
        (["0.5", "007.5"], "string"),
        (["1", "0", "1", "0"], "integer"),
        (["S-1001", "S-1002"], "string"),
        (COLORS, "enum"),
        ([LONG, "short"], "text"),
        ([], "string"),
        ([None, "", "NULL"], "string"),
    ],
)
def test_type_detection(values: list[object], expected: str) -> None:
    analysis = analyze(values)
    assert analysis.type.describe() == expected or analysis.type.name.value == expected


def test_type_precedence_prefers_the_narrowest_type() -> None:
    assert analyze(["1", "2"]).type == TypeSpec.of(AttributeType.INTEGER)
    assert analyze(["1", "2.5"]).type.name is AttributeType.DECIMAL
    assert analyze(["1", "2.123456"]).type.name is AttributeType.FLOAT
    assert analyze(["y", "n"]).type.name is AttributeType.BOOLEAN


def test_decimal_scale_threshold_is_an_option() -> None:
    assert analyze(["1.123456"], decimal_max_scale=6).type == TypeSpec.decimal(9, 6)
    assert analyze(["1.123456"], decimal_max_scale=4).type.name is AttributeType.FLOAT


def test_enum_needs_low_cardinality_and_enough_values() -> None:
    enum = analyze(COLORS)
    assert enum.type == TypeSpec.enum("blue", "green", "red")
    assert analyze(COLORS[:3]).type.name is AttributeType.STRING  # too few values
    assert analyze(COLORS, enum_max_cardinality=2).type.name is AttributeType.STRING
    assert analyze([f"v{i}" for i in range(20)]).type.name is AttributeType.STRING
    numbered = ["analyst-1", "analyst-2", "analyst-3"] * 5
    assert analyze(numbered).type.name is AttributeType.STRING


def test_text_by_length_or_by_name() -> None:
    prose = ["Audit found open critical findings.", "Two late delivery notices."] * 3
    assert analyze(prose, name="reason").type.name is AttributeType.TEXT
    assert analyze(prose, name="label").type.name is AttributeType.STRING
    assert analyze(["x" * 300]).type.name is AttributeType.TEXT
    assert analyze(["x" * 300], text_min_length=500).type.name is AttributeType.STRING


@pytest.mark.parametrize(
    ("values", "pattern"),
    [
        (["S-1001", "S-1002", "S-1003", "S-1004", "S-10005"], "^S-[0-9]{4,}$"),
        (["PL", "DE", "IT", "FR", "CZ"], "^[A-Z]{2}$"),
        (["PO-200001", "PO-200002", "PO-200003", "PO-200004", "PO-200005"], "^PO-[0-9]{6,}$"),
        (["AB12", "CD34", "EF56", "GH78", "IJ90"], "^[A-Z]{2}[0-9]{2,}$"),
        (["a.b1", "a.b2", "a.b3", "a.b4", "a.b5"], "^a\\.b[0-9]{1,}$"),
    ],
)
def test_patterns_of_code_like_values(values: list[str], pattern: str) -> None:
    assert shape_pattern(values) == pattern
    assert analyze(values).constraints.pattern == pattern


@pytest.mark.parametrize(
    "values",
    [
        ["Poznan", "Brno", "Milan", "Lyon", "Dresden"],
        ["S-1001", "S1002", "S-1003", "S-1004", "S-1005"],
        ["New York", "Old York", "Big York", "Low York", "Far York"],
    ],
)
def test_no_pattern_for_free_text(values: list[str]) -> None:
    assert shape_pattern(values) is None


def test_string_constraints() -> None:
    analysis = analyze(["abc", "abcdefghijklmnopq"])
    assert analysis.constraints.max_length == 32
    assert analyze(["x" * 200]).constraints.max_length == 256
    assert analyze(["abc"], propose_max_length=False).constraints.max_length is None


def test_numeric_min() -> None:
    assert analyze(["1", "2"]).constraints.min == 0
    assert analyze(["-1", "2"]).constraints.min is None
    assert analyze(["1", "2"], propose_non_negative_min=False).constraints.min is None


@pytest.mark.parametrize(
    ("values", "required", "unique"),
    [
        (["a", "b", "c"], True, True),
        (["a", "a", "c"], True, False),
        (["a", None, "c"], False, True),
        (["a", "", "c"], False, True),
        (["a"], True, False),
        ([], False, False),
    ],
)
def test_nullability_and_uniqueness(values: list[object], required: bool, unique: bool) -> None:
    analysis = analyze(values)
    assert analysis.required is required
    assert analysis.unique is unique


def test_null_ratio_threshold() -> None:
    values: list[object] = ["a"] * 99 + [None]
    assert analyze(values).required is False
    assert analyze(values, required_max_null_ratio=0.05).required is True


def test_mixed_types_are_reported() -> None:
    values: list[object] = [str(i) for i in range(19)] + ["n/a"]
    kept = analyze(values)
    assert kept.type.name is AttributeType.STRING
    assert kept.mixed is not None
    assert kept.mixed.dominant is AttributeType.INTEGER
    assert kept.mixed.outliers == ("n/a",)
    assert not kept.mixed.accepted
    tolerated = analyze(values, type_tolerance=0.1)
    assert tolerated.type.name is AttributeType.INTEGER
    assert tolerated.mixed is not None
    assert tolerated.mixed.accepted


def test_declared_facts_win() -> None:
    profile = ColumnProfile(
        "code",
        ["1", "2"],
        declared_type=TypeSpec.of(AttributeType.STRING),
        declared_max_length=10,
        nullable=True,
        unique=True,
    )
    analysis = analyze_column(profile, 0, OPTIONS)
    assert analysis.type == TypeSpec.of(AttributeType.STRING)
    assert analysis.constraints.max_length == 10
    assert analysis.required is False
    assert analysis.unique is True
    assert analysis.confidence == 1.0


def test_sample_size_cuts_long_columns() -> None:
    analysis = analyze_column(
        ColumnProfile("n", iter(range(1000))), 0, InferenceOptions(sample_size=10)
    )
    assert analysis.count == 10


def test_generated_ids() -> None:
    assert analyze(["01JZ72C4HR2QRFWCVF58JF4E0G", "01JZA84NH8NRV3HQWNMETSSK2G"]).generated_ids
    assert analyze(["123e4567-e89b-12d3-a456-426614174000"]).generated_ids
    assert not analyze(["S-1001"]).generated_ids


def test_confidence_grows_with_support() -> None:
    few = analyze(["1", "2"])
    many = analyze([str(i) for i in range(40)])
    assert few.confidence < many.confidence == 1.0
