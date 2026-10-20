"""Evaluation semantics, the sandbox budget and determinism."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import JsonValue

from lorebase_core.expr import (
    ExprError,
    ExprErrorCode,
    ExprLimits,
    LinkedRecord,
    MemoryResolver,
    Position,
    RecordNotLoaded,
    activation,
    compile,
    record_environment,
    run,
)
from lorebase_core.model import LinkDirection, Record

from .support import NOW, kind, record, supply, values

CONTRACT = record(
    "Contract",
    "C-3001",
    {
        "contract_number": "C-3001",
        "title": "Bearings framework 2026",
        "status": "draft",
        "start_date": "2026-01-01",
        "end_date": "2026-12-31",
        "annual_value": "600000.10",
        "payment_terms_days": "30",
    },
)


def evaluate(
    source: str,
    subject: Record = CONTRACT,
    *,
    resolver: MemoryResolver | None = None,
    limits: ExprLimits | None = None,
) -> object:
    environment = record_environment(supply(), subject.kind)
    compiled = compile(source, environment, limits=limits)
    return run(
        compiled, activation(environment, record=subject), now=NOW, resolver=resolver, limits=limits
    )


def failure(source: str, **options: object) -> ExprError:
    with pytest.raises(ExprError) as raised:
        evaluate(source, **options)  # type: ignore[arg-type]
    return raised.value


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("annual_value * 2", Decimal("1200000.20")),
        ('decimal("0.1") + decimal("0.2") == decimal("0.3")', True),
        ("0.1 + 0.2 == 0.3", False),
        ("annual_value > 600000", True),
        ("annual_value > 600000.5", False),
        ("round(annual_value / 12, 2)", Decimal("50000.01")),
        ("-7 / 2", -3),
        ("-7 % 2", -1),
        ("payment_terms_days + 1 == 31", True),
        ("1 == 1.0", True),
        ("status == 'draft' && status in ['draft', 'active']", True),
        ("title.contains('framework') && title.startsWith('Bear')", True),
        ("title.matches('^B[a-z]+ ') && !title.matches('^X')", True),
        ("title.split(' ').join('-')", "Bearings-framework-2026"),
        ("size(title) + title.size()", 46),
        ("title.lower().upper().substring(0, 4)", "BEAR"),
        ("string(annual_value) + ' ' + string(start_date)", "600000.10 2026-01-01"),
        ("end_date - start_date == days(364)", True),
        ("start_date + days(31)", date(2026, 2, 1)),
        ("days_between(start_date, end_date)", 364),
        ("end_date.getMonth() == 11 && end_date.getDate() == 31", True),
        ("today()", NOW.date()),
        ("now() - duration('1h30m')", NOW - timedelta(hours=1, minutes=30)),
        ("timestamp(start_date) < now()", True),
        ("[1, 2, 3].exists(x, x > 2) && [1, 2, 3].all(x, x > 0)", True),
        ("[1, 2, 3].exists_one(x, x > 1)", False),
        ("[1, 2, 3].filter(x, x > 1).map(x, x * 10)", [20, 30]),
        ("[1, 2, 3].count(x, x != 2) + count([1])", 3),
        ("sum([decimal('1.5'), 2]) == decimal('3.5')", True),
        ("max([3, 9, 4]) + min(2, 5)", 11),
        ("{'a': 1, 'b': 2}['b'] + {'a': 1}.a", 3),
        ("'a' in {'a': 1} && !(3 in [1, 2])", True),
        ("incoterm == null && !has({'x': 1}.y)", True),
        ("key + '/' + string(annual_value > 1 ? 'big' : 'small')", "C-3001/big"),
        ("1 / 0 == 1 || true", True),
        ("false && 1 / 0 == 1", False),
    ],
)
def test_expressions_evaluate(source: str, expected: object) -> None:
    result = evaluate(source)
    assert result == expected
    assert type(result) is type(expected)


@pytest.mark.parametrize(
    ("source", "fragment", "position"),
    [
        ("1 / 0", "division by zero", Position(1, 1)),
        ("9223372036854775807 + 1", "int64 range", Position(1, 1)),
        ("incoterm < 'DAP'", "has()", Position(1, 1)),
        ("start_date + hours(1)", "whole days", Position(1, 1)),
        ("[1][3]", "out of range", Position(1, 1)),
        ("int('x1')", "cannot parse", Position(1, 1)),
        (
            "title.matches(string(payment_terms_days) + '(')",
            "invalid regular expression",
            Position(1, 7),
        ),
        ("1 / 0 == 1 && true", "division by zero", Position(1, 1)),
    ],
)
def test_evaluation_errors_are_positioned(source: str, fragment: str, position: Position) -> None:
    error = failure(source)
    assert error.code is ExprErrorCode.EVALUATION
    assert fragment in error.message
    assert error.position == position
    assert error.source == source


def test_budget_limits_are_enforced() -> None:
    long_list = "[" + ", ".join(["1"] * 50) + "]"
    steps = failure(f"{long_list}.map(x, x + 1).size() > 0", limits=ExprLimits(max_steps=60))
    assert steps.code is ExprErrorCode.LIMIT_EXCEEDED
    assert "steps" in steps.message
    size = failure(f"{long_list}.map(x, x)", limits=ExprLimits(max_list_size=10))
    assert size.code is ExprErrorCode.LIMIT_EXCEEDED
    text = failure("title + title", limits=ExprLimits(max_string_length=30))
    assert text.code is ExprErrorCode.LIMIT_EXCEEDED
    assert evaluate(f"{long_list}.map(x, x + 1).size()") == 50


def test_links_read_one_hop_through_the_resolver() -> None:
    part = record("Part", "P-10001", {"part_number": "P-10001", "supplier_count": "2"})
    relation = supply().relation_by_api_name("supplied_by")
    assert relation is not None
    status = kind("Supplier").attribute_by_api_name("status")
    assert status is not None
    linked = [
        LinkedRecord("S-1001", {status.id: "active"}),
        LinkedRecord("S-1002", {status.id: "terminated"}),
    ]
    resolver = MemoryResolver(links={(relation[0].id, LinkDirection.FORWARD, "P-10001"): linked})
    source = 'links.supplied_by.filter(s, s.status == "active").map(s, s.key)'
    assert evaluate(source, part, resolver=resolver) == ["S-1001"]
    assert evaluate("size(links.supplied_by) == supplier_count", part, resolver=resolver) is True
    with pytest.raises(RecordNotLoaded) as missing:
        evaluate("size(links.stocked_at) > 0", part, resolver=resolver)
    assert missing.value.request.key == "P-10001"
    lookups = failure(
        "size(links.supplied_by) + size(links.supplied_by)",
        subject=part,
        resolver=resolver,
        limits=ExprLimits(max_record_lookups=1),
    )
    assert lookups.code is ExprErrorCode.LIMIT_EXCEEDED


def test_evaluation_is_deterministic_and_clock_injected() -> None:
    environment = record_environment(supply(), CONTRACT.kind)
    compiled = compile("string(now()) + ' ' + string(today())", environment)
    variables = activation(environment, record=CONTRACT)
    first = run(compiled, variables, now=NOW)
    later = run(compiled, variables, now=NOW + timedelta(days=1))
    assert first == run(compiled, variables, now=NOW) == "2026-10-21T09:30:00.000000Z 2026-10-21"
    assert later != first
    with pytest.raises(ValueError, match="timezone-aware"):
        run(compiled, variables, now=NOW.replace(tzinfo=None))


def test_stored_values_decode_with_their_types() -> None:
    raw: dict[str, JsonValue] = {"auto_renew": True, "currency": "EUR"}
    subject = Record(
        kind=CONTRACT.kind, key="C-1", values={**CONTRACT.values, **values("Contract", raw)}
    )
    assert evaluate("auto_renew && currency == 'EUR' && payment_terms_days == 30", subject) is True
