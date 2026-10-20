"""Parsing and static type checking against environments derived from the supply schema."""

from __future__ import annotations

import pytest

from lorebase_core.expr import (
    Environment,
    ExprError,
    ExprErrorCode,
    ExprLimits,
    Position,
    TypeKind,
    compile,
    operation_environment,
    record_environment,
)
from lorebase_core.expr.ast import rewrite_step_references
from lorebase_core.expr.types import BOOL, from_type_spec
from lorebase_core.model import Operation

from .support import kind, supply


def flag() -> Operation:
    operation = supply().operation_by_api_name("flag_supplier_risk")
    assert operation is not None
    return operation


def operation_env(step: int | None = None) -> Environment:
    return operation_environment(supply(), flag(), step_index=step)


def contract_env() -> Environment:
    return record_environment(supply(), kind("Contract").id)


def failure(source: str, environment: Environment, **options: object) -> ExprError:
    with pytest.raises(ExprError) as raised:
        compile(source, environment, **options)  # type: ignore[arg-type]
    return raised.value


def test_supply_expressions_check_with_their_declared_types() -> None:
    env = operation_env()
    assert compile('record(supplier).status != "terminated"', env, expected=BOOL).type == BOOL
    assert compile('params.severity == "critical"', env).type == BOOL
    assert compile("now()", env).type.kind is TypeKind.TIMESTAMP
    assert compile("principal.id", env).type.kind is TypeKind.STRING
    step = compile("$step[0]", operation_env(1))
    assert step.type.kind is TypeKind.REF
    assert step.type.name == kind("RiskFlag").id


@pytest.mark.parametrize(
    ("source", "code", "position", "fragment"),
    [
        ("1 + ", ExprErrorCode.SYNTAX, Position(1, 5), "unexpected end"),
        ("a $ b", ExprErrorCode.SYNTAX, Position(1, 3), "$step[n]"),
        ("1u", ExprErrorCode.SYNTAX, Position(1, 1), "unsigned"),
        ("Supplier{status: 1}", ExprErrorCode.SYNTAX, Position(1, 1), "message construction"),
        ("params.severty", ExprErrorCode.TYPE, Position(1, 8), "did you mean 'severity'"),
        ("record(supplier).statuss", ExprErrorCode.TYPE, Position(1, 18), "Kind Supplier"),
        ('record(supplier).status == "gone"', ExprErrorCode.TYPE, Position(1, 1), "'gone'"),
        ("params.supplier.status", ExprErrorCode.TYPE, Position(1, 17), "record(...).status"),
        ("params.severity + 1", ExprErrorCode.TYPE, Position(1, 1), "operator +"),
        ("record(params.reason)", ExprErrorCode.TYPE, Position(1, 1), "Record reference"),
        ("supplierr", ExprErrorCode.TYPE, Position(1, 1), "did you mean 'supplier'"),
        ("$step[0]", ExprErrorCode.TYPE, Position(1, 1), "only available in Operation steps"),
        ('params.reason.matches("(")', ExprErrorCode.TYPE, Position(1, 15), "regular expression"),
        ('links(supplier, "for_part")', ExprErrorCode.TYPE, Position(1, 1), "does not start"),
        ("true ? 1 : 'a'", ExprErrorCode.TYPE, Position(1, 1), "different types"),
        ("[1, 2].all(x, x)", ExprErrorCode.TYPE, Position(1, 15), "bool condition"),
    ],
)
def test_errors_carry_code_position_and_message(
    source: str, code: ExprErrorCode, position: Position, fragment: str
) -> None:
    error = failure(source, operation_env())
    assert error.code is code
    assert error.position == position
    assert fragment in error.message
    assert error.source == source
    assert error.describe().startswith(f"{position}: ")


def test_steps_see_only_earlier_steps() -> None:
    assert "does not name an earlier step" in failure("$step[1]", operation_env(1)).message
    assert "edits a Link" in failure("$step[1]", operation_env(2)).message
    assert "literal step number" in failure("$step[size([1])]", operation_env(1)).message


def test_expected_type_is_enforced() -> None:
    env = operation_env()
    error = failure("params.reason", env, expected=BOOL)
    assert error.message == "expression has type string, expected bool"
    severity = kind("RiskFlag").attribute_by_api_name("severity")
    assert severity is not None
    target = from_type_spec(severity.type)
    assert compile('"high"', env, expected=target).type.values == frozenset({"high"})
    assert "expected enum" in failure('"urgent"', env, expected=target).message
    assert compile("null", env, expected=target, nullable=True).type.kind is TypeKind.NULL


def test_numbers_do_not_mix_silently() -> None:
    env = contract_env()
    assert compile("annual_value * 2", env).type.kind is TypeKind.DECIMAL
    assert compile("annual_value > 500000", env).type == BOOL
    assert compile("annual_value > 0.5", env).type == BOOL  # comparison across kinds
    assert "convert with decimal()" in failure("annual_value * 1.5", env).message
    assert "convert with double() or int()" in failure("payment_terms_days * 1.5", env).message


def test_record_environment_exposes_attributes_key_and_links() -> None:
    env = record_environment(supply(), kind("Part").id)
    assert compile("supplier_count == 1", env).type == BOOL
    assert compile("key.startsWith('P-')", env).type == BOOL
    links = compile('links.supplied_by.exists(s, s.status == "active")', env)
    assert links.type == BOOL
    error = failure("record(key)", env)
    assert "not available here" in error.message


def test_macros_and_library_types() -> None:
    env = contract_env()
    assert compile("[1, 2].map(x, x * 2)", env).type.element.kind is TypeKind.INT
    assert compile("[1, 2].filter(x, x > 1)", env).type.kind is TypeKind.LIST
    assert compile("[1, 2].count(x, x > 1)", env).type.kind is TypeKind.INT
    assert compile("has(record(supplier).city)", operation_env()).type == BOOL
    assert compile("end_date - start_date", env).type.kind is TypeKind.DURATION
    assert compile("start_date + days(30)", env).type.kind is TypeKind.DATE
    assert compile("round(annual_value / 12, 2)", env).type.kind is TypeKind.DECIMAL
    assert compile("max([1, 2, 3])", env).type.kind is TypeKind.INT
    assert "unknown function" in failure("frobnicate(1)", env).message
    assert "call it as x.contains()" in failure("contains(title, 'a')", env).message


def test_limits_bound_compilation() -> None:
    env = contract_env()
    error = failure("1 + 1", env, limits=ExprLimits(max_source_length=3))
    assert error.code is ExprErrorCode.LIMIT_EXCEEDED
    nested = "(" * 30 + "1" + ")" * 30
    assert compile(nested, env).type.kind is TypeKind.INT  # parentheses do not nest the tree
    deep = "-" * 20 + "1"
    error = failure(" - ".join(["1"] * 3) + " + " + deep, env, limits=ExprLimits(max_depth=8))
    assert error.code is ExprErrorCode.LIMIT_EXCEEDED


def test_step_references_are_rewritten_outside_strings_only() -> None:
    source = "$step[0] == '$step' && \"a\\\"$step\" != r'$step'"
    rewritten = rewrite_step_references(source)
    assert rewritten.startswith("_step[0]")
    assert rewritten.count("$step") == 3
    assert len(rewritten) == len(source)
