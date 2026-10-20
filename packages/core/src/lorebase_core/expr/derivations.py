"""Derivations (design 5.8): compile ``expression`` Derivations and aggregate filters, evaluate
them over one Record.

Recomputing derived values when Records change is the materializer's job; this module only
computes one value from one Record (whose values may include other derived values).
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import JsonValue

from lorebase_core.expr.compiler import CompiledExpression, compile, run
from lorebase_core.expr.context import EvaluationContext, activation
from lorebase_core.expr.environment import LINKS, record_environment
from lorebase_core.expr.errors import ExprError, ExprErrorCode, PathPart
from lorebase_core.expr.limits import ExprLimits
from lorebase_core.expr.ops import require_bool
from lorebase_core.expr.types import BOOL, from_type_spec
from lorebase_core.expr.values import encode_result
from lorebase_core.model.derivation import AggregateDerivation, Derivation, ExpressionDerivation
from lorebase_core.model.record import Record
from lorebase_core.model.schema import Schema
from lorebase_core.types import TypeSpec


@dataclass(frozen=True, eq=False)
class CompiledDerivation:
    """An ``expression`` Derivation (``expression`` and ``target`` set) or an aggregate with a
    ``filter`` over the Records it aggregates."""

    derivation: Derivation
    expression: CompiledExpression | None = None
    target: TypeSpec | None = None
    filter: CompiledExpression | None = None


def _compile(
    schema: Schema, derivation: Derivation, limits: ExprLimits, errors: list[ExprError] | None
) -> CompiledDerivation:
    definition = derivation.definition

    def fail(error: ExprError, path: tuple[PathPart, ...]) -> None:
        error.located(path=path)
        if errors is None:
            raise error
        errors.append(error)

    if isinstance(definition, ExpressionDerivation):
        kind = schema.kinds.get(definition.kind)
        attribute = kind.attributes.get(definition.attribute) if kind is not None else None
        if kind is None or attribute is None:
            if errors is None:
                raise ExprError(ExprErrorCode.TYPE, "the Derivation's target does not exist")
            return CompiledDerivation(derivation)
        if LINKS in {a.api_name for a in kind.attributes.values()}:
            fail(
                ExprError(
                    ExprErrorCode.TYPE, f"attribute name {LINKS!r} is reserved in expressions"
                ),
                ("definition", "kind"),
            )
        try:
            program = compile(
                definition.expression,
                record_environment(schema, kind.id),
                expected=from_type_spec(attribute.type),
                nullable=True,
                limits=limits,
            )
        except ExprError as error:
            fail(error, ("definition", "expression"))
            return CompiledDerivation(derivation)
        return CompiledDerivation(derivation, expression=program, target=attribute.type)
    if isinstance(definition, AggregateDerivation) and definition.filter is not None:
        relation = schema.relations.get(definition.relation)
        if relation is None:
            if errors is None:
                raise ExprError(ExprErrorCode.TYPE, "the aggregated Relation does not exist")
            return CompiledDerivation(derivation)
        far = relation.far_endpoint(definition.direction)
        try:
            program = compile(
                definition.filter,
                record_environment(schema, far),
                expected=BOOL,
                limits=limits,
            )
        except ExprError as error:
            fail(error, ("definition", "filter"))
            return CompiledDerivation(derivation)
        return CompiledDerivation(derivation, filter=program)
    return CompiledDerivation(derivation)


def compile_derivation(
    schema: Schema, derivation: Derivation, limits: ExprLimits | None = None
) -> CompiledDerivation:
    """Compile the expressions of ``derivation``; raises the first ``ExprError``."""
    return _compile(schema, derivation, limits or ExprLimits(), None)


def derivation_expression_errors(
    schema: Schema, derivation: Derivation, limits: ExprLimits | None = None
) -> list[ExprError]:
    """Every expression problem of ``derivation`` (paths inside the element)."""
    errors: list[ExprError] = []
    _compile(schema, derivation, limits or ExprLimits(), errors)
    return errors


def evaluate_derivation(
    compiled: CompiledDerivation, record: Record, context: EvaluationContext
) -> JsonValue:
    """The canonical value an ``expression`` Derivation computes for ``record``.

    ``None`` means no value. Raises ``ExprError`` when evaluation fails or the result does
    not fit the target Attribute (``expression.value_invalid``).
    """
    if compiled.expression is None or compiled.target is None:
        raise ExprError(ExprErrorCode.EVALUATION, "not an expression Derivation")
    program = compiled.expression
    variables = activation(program.environment, record=record)
    value = run(
        program,
        variables,
        now=context.now,
        resolver=context.resolver,
        limits=context.limits,
    )
    return encode_result(compiled.target, value)


def matches_filter(
    compiled: CompiledDerivation, record: Record, context: EvaluationContext
) -> bool:
    """Whether a Record at the far end of an aggregate passes its ``filter`` (true if none)."""
    if compiled.filter is None:
        return True
    variables = activation(compiled.filter.environment, record=record)
    value = run(
        compiled.filter,
        variables,
        now=context.now,
        resolver=context.resolver,
        limits=context.limits,
    )
    return require_bool(value, "filter")
