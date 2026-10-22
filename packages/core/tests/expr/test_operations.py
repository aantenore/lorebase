"""Operations and Derivations of the supply schema: preconditions, review policy, steps."""

from __future__ import annotations

import pytest
from pydantic import JsonValue

from lorebase_core.expr import (
    CompiledOperation,
    ExprError,
    ExprErrorCode,
    MemoryResolver,
    RecordNotLoaded,
    RecordRequest,
    check_preconditions,
    compile_derivation,
    compile_operation,
    evaluate_derivation,
    evaluate_review_policy,
    operation_expression_errors,
    plan_steps,
)
from lorebase_core.model import (
    CreateRecordStep,
    Operation,
    OperationParameter,
    Precondition,
    UpdateRecordStep,
)
from lorebase_core.types import TypeSpec
from lorebase_core.validate import LinkUpsert, RecordUpsert

from .support import context, kind, record, supply, values

OPERATION_ID = "opr_01KEZF7C00000000000000000A"


def compiled(name: str) -> CompiledOperation:
    operation = supply().operation_by_api_name(name)
    assert operation is not None
    return compile_operation(supply(), operation)


def supplier_status(status: str) -> MemoryResolver:
    return MemoryResolver(
        records={(kind("Supplier").id, "S-1001"): values("Supplier", {"status": status})}
    )


def contract(status: str, annual_value: str = "250000.00") -> MemoryResolver:
    existing = values(
        "Contract",
        {
            "contract_number": "C-3001",
            "title": "Seals 2026",
            "status": status,
            "start_date": "2026-01-01",
            "annual_value": annual_value,
        },
    )
    return MemoryResolver(records={(kind("Contract").id, "C-3001"): existing})


FLAG: dict[str, JsonValue] = {"supplier": "S-1001", "severity": "high", "reason": "Late twice"}


def test_flag_supplier_risk_plans_a_flag_and_its_link() -> None:
    operation = compiled("flag_supplier_risk")
    ctx = context(supplier_status("active"))
    assert check_preconditions(operation, FLAG, ctx) == []
    assert evaluate_review_policy(operation, FLAG, ctx) is False
    assert evaluate_review_policy(operation, {**FLAG, "severity": "critical"}, ctx) is True
    plan = plan_steps(operation, FLAG, ctx)
    created, linked = plan.changes
    assert isinstance(created, RecordUpsert)
    assert isinstance(linked, LinkUpsert)
    flag_key = created.record.key
    assert created.record.kind == kind("RiskFlag").id
    assert created.record.values == values(
        "RiskFlag",
        {
            "severity": "high",
            "reason": "Late twice",
            "raised_at": "2026-10-21T09:30:00.000000Z",
            "raised_by": "user:ana",
        },
    )
    assert (linked.link.from_key, linked.link.to_key) == (flag_key, "S-1001")
    assert [ref.key if ref else None for ref in plan.records] == [flag_key, None]
    assert plan_steps(operation, FLAG, context(supplier_status("active"))) == plan  # deterministic


def test_flag_supplier_risk_refuses_terminated_suppliers() -> None:
    operation = compiled("flag_supplier_risk")
    [failure] = check_preconditions(operation, FLAG, context(supplier_status("terminated")))
    assert failure.index == 0
    assert failure.message == "Terminated suppliers cannot be flagged."
    assert failure.error is None


def test_missing_records_are_requested_or_reported() -> None:
    operation = compiled("flag_supplier_risk")
    with pytest.raises(RecordNotLoaded) as raised:
        check_preconditions(operation, FLAG, context(MemoryResolver()))
    assert raised.value.request == RecordRequest(kind("Supplier").id, "S-1001")
    absent = MemoryResolver(records={(kind("Supplier").id, "S-1001"): None})
    [failure] = check_preconditions(operation, FLAG, context(absent))
    assert failure.error is not None
    assert failure.error.code is ExprErrorCode.RECORD_NOT_FOUND
    assert failure.error.path == ("preconditions", 0, "expression")


def test_approve_contract_updates_the_draft() -> None:
    operation = compiled("approve_contract")
    parameters: dict[str, JsonValue] = {"contract": "C-3001"}
    ctx = context(contract("draft"))
    assert check_preconditions(operation, parameters, ctx) == []
    assert evaluate_review_policy(operation, parameters, ctx) is False
    assert evaluate_review_policy(operation, parameters, context(contract("draft", "750000.00")))
    [change] = plan_steps(operation, parameters, ctx).changes
    assert isinstance(change, RecordUpsert)
    expected = values(
        "Contract",
        {
            "contract_number": "C-3001",
            "title": "Seals 2026",
            "status": "active",
            "start_date": "2026-01-01",
            "annual_value": "250000.00",
            "approved_by": "user:ana",
            "approved_at": "2026-10-21T09:30:00.000000Z",
        },
    )
    assert change.record.values == expected
    [failure] = check_preconditions(operation, parameters, context(contract("active")))
    assert failure.message == "Only draft contracts can be approved."


def test_step_failures_carry_the_step_path() -> None:
    operation = compiled("approve_contract")
    missing = MemoryResolver(records={(kind("Contract").id, "C-9"): None})
    with pytest.raises(ExprError) as raised:
        plan_steps(operation, {"contract": "C-9"}, context(missing))
    assert raised.value.code is ExprErrorCode.RECORD_NOT_FOUND
    assert raised.value.path == ("steps", 0, "target")


def _custom(**fields: object) -> Operation:
    parameters = (
        OperationParameter(name="supplier", type=TypeSpec.ref(kind("Supplier").id)),
        OperationParameter(name="score", type=TypeSpec.of("integer")),
    )
    fields.setdefault("parameters", parameters)
    return Operation(
        id=OPERATION_ID,
        api_name="rescore_supplier",
        description="Set a supplier's risk score.",
        **fields,  # type: ignore[arg-type]
    )


def _score_step(expression: str) -> UpdateRecordStep:
    score = kind("Supplier").attribute_by_api_name("risk_score")
    assert score is not None
    return UpdateRecordStep(
        kind=kind("Supplier").id, target="params.supplier", values={score.id: expression}
    )


def test_values_that_do_not_fit_are_refused() -> None:
    operation = compile_operation(supply(), _custom(steps=(_score_step("params.score * 2"),)))
    ctx = context(supplier_status("active"))
    plan = plan_steps(operation, {"supplier": "S-1001", "score": "40"}, ctx)
    assert isinstance(plan.changes[0], RecordUpsert)
    with pytest.raises(ExprError) as raised:
        plan_steps(operation, {"supplier": "S-1001", "score": "9223372036854775807"}, ctx)
    assert raised.value.code is ExprErrorCode.EVALUATION
    assert "int64" in raised.value.message
    assert raised.value.path[:3] == ("steps", 0, "values")
    flag = kind("RiskFlag")
    severity = flag.attribute_by_api_name("severity")
    assert severity is not None
    open_string = CreateRecordStep(kind=flag.id, values={severity.id: "principal.id"})
    operation = compile_operation(supply(), _custom(steps=(open_string,)))
    with pytest.raises(ExprError) as raised:
        plan_steps(operation, {"supplier": "S-1001", "score": "1"}, ctx)
    assert raised.value.code is ExprErrorCode.VALUE_INVALID
    assert raised.value.message.startswith("severity: 'user:ana' is not one of")
    assert raised.value.path == ("steps", 0, "values", severity.id)
    # Located with the value's expression, so what it reads can be told (redaction).
    assert raised.value.source == "principal.id"


def test_later_steps_read_records_of_earlier_steps() -> None:
    flag = kind("RiskFlag")
    attribute = {a.api_name: a.id for a in flag.attributes.values()}
    first = CreateRecordStep(
        kind=flag.id,
        values={
            attribute["severity"]: '"low"',
            attribute["reason"]: '"copied"',
            attribute["raised_at"]: "now()",
            attribute["raised_by"]: "principal.id",
        },
    )
    second = CreateRecordStep(
        kind=flag.id,
        values={
            attribute["severity"]: "record($step[0]).severity",
            attribute["reason"]: "record($step[0]).reason + ' again'",
            attribute["raised_at"]: "record($step[0]).raised_at",
            attribute["raised_by"]: "record($step[0]).raised_by",
        },
    )
    operation = compile_operation(supply(), _custom(steps=(first, second)))
    plan = plan_steps(operation, {"supplier": "S-1001", "score": "1"}, context())
    copy = plan.changes[1]
    assert isinstance(copy, RecordUpsert)
    assert copy.record.values[attribute["reason"]] == "copied again"


def test_compile_errors_collect_paths() -> None:
    broken = _custom(
        preconditions=(Precondition(expression="params.scor > 1", message="m"),),
        steps=(_score_step('"high"'),),
    )
    errors = operation_expression_errors(supply(), broken)
    assert [e.path for e in errors] == [
        ("preconditions", 0, "expression"),
        ("steps", 0, "values", kind("Supplier").attribute_by_api_name("risk_score").id),  # type: ignore[union-attr]
    ]
    with pytest.raises(ExprError) as raised:
        compile_operation(supply(), broken)
    assert raised.value.path == ("preconditions", 0, "expression")
    reserved = _custom(
        parameters=(OperationParameter(name="principal", type=TypeSpec.of("string")),)
    )
    assert operation_expression_errors(supply(), reserved)[0].path == ("parameters", 0, "name")


def test_single_sourced_derivation() -> None:
    derivation = supply().derivation_by_api_name("single_sourced")
    assert derivation is not None
    program = compile_derivation(supply(), derivation)
    one = record("Part", "P-10001", {"part_number": "P-10001", "supplier_count": "1"})
    two = record("Part", "P-10002", {"part_number": "P-10002", "supplier_count": "2"})
    assert evaluate_derivation(program, one, context()) is True
    assert evaluate_derivation(program, two, context()) is False
    none = record("Part", "P-10003", {"part_number": "P-10003"})
    assert evaluate_derivation(program, none, context()) is False
