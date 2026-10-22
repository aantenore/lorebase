"""Expressions over the rows of a Source."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from lorebase_core.expr import (
    ExprError,
    RowExpressions,
    compile_row_expression,
    evaluate_row_expression,
    row_value,
    row_variables,
)
from lorebase_core.expr import rows as row_module

from .support import NOW, supply


def run(expression: str, row: dict[str, object]) -> object:
    compiled = compile_row_expression(supply(), expression, row)
    return evaluate_row_expression(compiled, row, now=NOW)


def test_identifier_columns_are_variables_and_row_holds_them_all() -> None:
    row = {"first": "Ada", "last": "Lovelace", "Legal name": "Analytical Engines"}
    assert run('first + " " + last', row) == "Ada Lovelace"
    assert run('row["Legal name"].upper()', row) == "ANALYTICAL ENGINES"
    assert row_variables(row) == ["first", "last"]


def test_values_are_dynamic() -> None:
    assert run("int(price) * 2", {"price": "21"}) == 42
    assert run("qty * 3", {"qty": 4}) == 12
    assert run("note == null ? 'none' : note", {"note": None}) == "none"
    assert run("has(row.missing) ? 1 : 0", {"price": "1"}) == 0


def test_reserved_names_are_not_variables() -> None:
    assert row_variables(["true", "row", "in", "ok"]) == ["ok"]


def test_a_column_that_is_not_there_does_not_compile() -> None:
    with pytest.raises(ExprError) as undeclared:
        compile_row_expression(supply(), "price + 1", ["cost"])
    assert undeclared.value.code.value == "expression.type"
    with pytest.raises(ExprError) as syntax:
        compile_row_expression(supply(), "price +", ["price"])
    assert syntax.value.code.value == "expression.syntax"


def test_evaluation_errors_are_reported() -> None:
    with pytest.raises(ExprError) as failed:
        run("int(price)", {"price": "abc"})
    assert failed.value.code.value == "expression.evaluation"


def test_results_have_a_json_form() -> None:
    assert row_value(Decimal("1.50")) == "1.50"
    assert row_value([1, "a", None]) == [1, "a", None]
    assert row_value(NOW) == NOW.isoformat()
    assert row_value(NOW.date()) == NOW.date().isoformat()
    assert run("[price, price]", {"price": 2}) == [2, 2]


def test_row_expressions_compile_once_over_the_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    compiled: list[tuple[str, tuple[str, ...]]] = []
    original = row_module.compile_row_expression

    def counting(schema: Any, expression: str, columns: Any, limits: Any = None) -> Any:
        compiled.append((expression, tuple(columns)))
        return original(schema, expression, columns, limits)

    monkeypatch.setattr(row_module, "compile_row_expression", counting)
    programs = RowExpressions(supply(), ["price", "Legal name"])
    assert programs.evaluate("price", {"price": 1}, now=NOW) == 1
    assert programs.evaluate("price", {"price": 2, "Legal name": "x"}, now=NOW) == 2
    assert compiled == [("price", ("price", "Legal name"))]
    # A declared column the row lacks is null, as a variable and in `row`.
    assert programs.evaluate("price == null", {"Legal name": "x"}, now=NOW) is True
    assert programs.evaluate('row["Legal name"] == null', {"price": 3}, now=NOW) is True
    assert len(compiled) == 3  # one program per expression
    # A new column compiles again over the larger set.
    assert programs.evaluate("price", {"price": 4, "qty": 1}, now=NOW) == 4
    assert compiled[-1] == ("price", ("price", "Legal name", "qty"))
    assert programs.columns == ("price", "Legal name", "qty")


def test_row_expressions_keep_compile_errors() -> None:
    programs = RowExpressions(supply(), ["price"])
    for _ in range(2):
        with pytest.raises(ExprError) as undeclared:
            programs.evaluate("cost + 1", {"price": 1}, now=NOW)
        assert undeclared.value.code.value == "expression.type"


def test_huge_decimals_keep_the_exponent_form() -> None:
    limit = row_module.PLAIN_DECIMAL_MAX_EXPONENT
    assert row_value(Decimal(f"1E+{limit}")) == "1" + "0" * limit
    assert row_value(Decimal(f"1E+{limit + 1}")) == f"1E+{limit + 1}"
    assert row_value(Decimal("1E+999999")) == "1E+999999"
    assert row_value(Decimal("-1E-999999")) == "-1E-999999"
    assert row_value(Decimal("NaN")) == "NaN"
