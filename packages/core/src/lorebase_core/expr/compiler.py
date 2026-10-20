"""Compile an expression (parse and type check) and run a compiled expression."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from lorebase_core.expr.ast import Node, parse
from lorebase_core.expr.checker import Checker
from lorebase_core.expr.environment import Environment
from lorebase_core.expr.errors import ExprError, type_error
from lorebase_core.expr.evaluator import Evaluator, Overlay
from lorebase_core.expr.limits import ExprLimits
from lorebase_core.expr.resolver import RecordResolver
from lorebase_core.expr.types import ExprType, assignable


@dataclass(frozen=True, eq=False)
class CompiledExpression:
    """A parsed and type-checked expression, ready to run any number of times."""

    source: str
    root: Node
    type: ExprType
    environment: Environment
    limits: ExprLimits


def compile(
    expression: str,
    environment: Environment,
    *,
    expected: ExprType | None = None,
    nullable: bool = False,
    limits: ExprLimits | None = None,
) -> CompiledExpression:
    """Parse and type check ``expression`` in ``environment``.

    With ``expected``, the result type must be assignable to it (``nullable`` also accepts
    ``null``). Raises ``ExprError`` with code ``expression.syntax``, ``expression.type`` or
    ``expression.limit_exceeded`` and the position of the problem.
    """
    limits = limits or ExprLimits()
    root = parse(expression, limits)
    try:
        result = Checker(environment).check(root)
        if expected is not None and not assignable(result, expected, nullable=nullable):
            raise type_error(
                f"expression has type {environment.describe(result)}, "
                f"expected {environment.describe(expected)}",
                root.pos,
            )
    except ExprError as error:
        raise error.located(source=expression) from None
    except RecursionError:
        raise type_error("expression nests too deeply to check").located(
            source=expression
        ) from None
    return CompiledExpression(expression, root, result, environment, limits)


def run(
    compiled: CompiledExpression,
    variables: Mapping[str, object],
    *,
    now: datetime,
    resolver: RecordResolver | None = None,
    limits: ExprLimits | None = None,
    overlay: Overlay | None = None,
) -> object:
    """Evaluate ``compiled`` with runtime ``variables``; returns the runtime value.

    Raises ``ExprError`` for evaluation problems and lets ``RecordNotLoaded`` through.
    """
    evaluator = Evaluator(
        compiled.environment,
        variables,
        now=now,
        limits=limits or compiled.limits,
        resolver=resolver,
        overlay=overlay,
    )
    try:
        return evaluator.evaluate(compiled.root)
    except ExprError as error:
        raise error.located(source=compiled.source) from None
