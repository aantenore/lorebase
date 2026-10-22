"""Derivations (design 5.8): compile ``expression`` Derivations and aggregate filters, evaluate
them over one Record, and tell which derived Attributes and Relations they read.

Recomputing derived values when Records change is the materializer's job; this module only
computes one value from one Record (whose values may include other derived values).
``derivation_inputs`` lists the derived Attributes a Derivation may read, for cycle detection
and evaluation order; ``links_read`` lists the ``links.<relation>`` fields an expression reads.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, fields

from pydantic import JsonValue

from lorebase_core.expr.ast import Ident, Node, Select, parse
from lorebase_core.expr.compiler import CompiledExpression, compile, run
from lorebase_core.expr.context import EvaluationContext, activation
from lorebase_core.expr.environment import LINKS, LinkField, link_fields, record_environment
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


# Dependencies ------------------------------------------------------------------------------------


def _nodes(node: Node) -> Iterator[Node]:
    """``node`` and every node below it."""
    yield node
    for item in fields(node):
        value = getattr(node, item.name)
        children = value if isinstance(value, tuple) else (value,)
        for child in children:
            for part in child if isinstance(child, tuple) else (child,):
                if isinstance(part, Node):
                    yield from _nodes(part)


@dataclass(frozen=True)
class _Names:
    """Identifiers, selected fields and ``links.<name>`` fields of an expression."""

    idents: frozenset[str]
    fields: frozenset[str]
    links: frozenset[str]


def _names(root: Node) -> _Names:
    idents: set[str] = set()
    selected: set[str] = set()
    links: set[str] = set()
    for node in _nodes(root):
        if isinstance(node, Ident):
            idents.add(node.name)
        elif isinstance(node, Select):
            selected.add(node.field)
            if isinstance(node.operand, Ident) and node.operand.name == LINKS:
                links.add(node.field)
    return _Names(frozenset(idents), frozenset(selected), frozenset(links))


def links_read(program: CompiledExpression) -> list[LinkField]:
    """The ``links.<relation>`` fields a Record expression reads, in name order."""
    names = _names(program.root).links
    available = program.environment.link_fields
    return [available[name] for name in sorted(names) if name in available]


def _record_inputs(schema: Schema, kind_id: str, source: str, limits: ExprLimits) -> set[str]:
    """Derived Attributes an expression over a Record of ``kind_id`` may read: its own derived
    Attributes named in it, and derived Attributes of Records one hop away (through a
    ``links.<relation>`` field) whose name appears in it. Conservative by design."""
    try:
        names = _names(parse(source, limits))
    except ExprError:
        return set()  # reported by expression validation
    words = names.idents | names.fields
    inputs: set[str] = set()
    kind = schema.kinds.get(kind_id)
    if kind is not None:
        inputs.update(
            a.id for a in kind.attributes.values() if a.is_derived and a.api_name in names.idents
        )
    available = link_fields(schema, kind_id)
    for name in names.links:
        field = available.get(name)
        far = (
            None
            if field is None
            else schema.kinds.get(field.relation.far_endpoint(field.direction))
        )
        if far is not None:
            inputs.update(
                a.id for a in far.attributes.values() if a.is_derived and a.api_name in words
            )
    return inputs


def derivation_inputs(
    schema: Schema, derivation: Derivation, limits: ExprLimits | None = None
) -> frozenset[str]:
    """Ids of the derived Attributes ``derivation`` may read (a superset, never fewer).

    An ``expression`` Derivation reads its Record and the Records one hop away; an aggregate
    reads the ``source_attribute`` and the ``filter`` of the Records it aggregates. Path
    Derivations read nothing derived.
    """
    limits = limits or ExprLimits()
    definition = derivation.definition
    if isinstance(definition, ExpressionDerivation):
        return frozenset(_record_inputs(schema, definition.kind, definition.expression, limits))
    if not isinstance(definition, AggregateDerivation):
        return frozenset()
    relation = schema.relations.get(definition.relation)
    if relation is None:
        return frozenset()
    far_id = relation.far_endpoint(definition.direction)
    far = schema.kinds.get(far_id)
    inputs: set[str] = set()
    if far is not None and definition.source_attribute is not None:
        source = far.attributes.get(definition.source_attribute)
        if source is not None and source.is_derived:
            inputs.add(source.id)
    if definition.filter is not None:
        inputs.update(_record_inputs(schema, far_id, definition.filter, limits))
    return frozenset(inputs)


def _expression_reads(
    schema: Schema, kind_id: str, source: str, limits: ExprLimits
) -> set[tuple[str, str]]:
    """`(Kind id, Attribute id)` of every Attribute an expression over a Record of `kind_id`
    may read: its own Attributes named in it and those of Records one hop away (through a
    ``links.<relation>`` field) whose name appears in it. A superset, never fewer."""
    try:
        names = _names(parse(source, limits))
    except ExprError:
        return set()  # reported by expression validation
    words = names.idents | names.fields
    reads: set[tuple[str, str]] = set()
    kind = schema.kinds.get(kind_id)
    if kind is not None:
        reads.update((kind_id, a.id) for a in kind.attributes.values() if a.api_name in words)
    available = link_fields(schema, kind_id)
    for name in names.links:
        field = available.get(name)
        far_id = None if field is None else field.relation.far_endpoint(field.direction)
        far = None if far_id is None else schema.kinds.get(far_id)
        if far is not None:
            reads.update((far.id, a.id) for a in far.attributes.values() if a.api_name in words)
    return reads


def derivation_reads(
    schema: Schema, derivation: Derivation, limits: ExprLimits | None = None
) -> frozenset[tuple[str, str]]:
    """`(Kind id, Attribute id)` of every Attribute, stored or derived, a Derivation may read
    (a superset): what its value can tell, for sensitivity (`schema.sensitivity_inherited`)."""
    limits = limits or ExprLimits()
    definition = derivation.definition
    if isinstance(definition, ExpressionDerivation):
        return frozenset(_expression_reads(schema, definition.kind, definition.expression, limits))
    if not isinstance(definition, AggregateDerivation):
        return frozenset()
    relation = schema.relations.get(definition.relation)
    if relation is None:
        return frozenset()
    far_id = relation.far_endpoint(definition.direction)
    reads: set[tuple[str, str]] = set()
    if definition.source_attribute is not None:
        reads.add((far_id, definition.source_attribute))
    if definition.filter is not None:
        reads.update(_expression_reads(schema, far_id, definition.filter, limits))
    return frozenset(reads)
