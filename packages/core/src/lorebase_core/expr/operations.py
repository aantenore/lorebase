"""Operations (design 5.7): compile their expressions, check preconditions, plan their steps.

Everything is pure: Records are read through the context's ``RecordResolver`` and the
result is a list of ``lorebase_core.validate`` data changes for the commit pipeline.

Steps run in order. A step sees the Records written by earlier steps (``record($step[0])``
reads the Record step 0 created), and ``$step[n]`` is the reference of the Record step
``n`` created, updated or deleted.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from pydantic import JsonValue

from lorebase_core.expr.ast import STEP_VARIABLE
from lorebase_core.expr.compiler import CompiledExpression, compile, run
from lorebase_core.expr.context import EvaluationContext, activation
from lorebase_core.expr.environment import PARAMS, PRINCIPAL, Environment, operation_environment
from lorebase_core.expr.errors import ExprError, ExprErrorCode, PathPart, evaluation_error
from lorebase_core.expr.limits import ExprLimits
from lorebase_core.expr.ops import ref_key, require_bool
from lorebase_core.expr.types import BOOL, ExprType, from_type_spec, ref_of
from lorebase_core.expr.values import Ref, encode_result
from lorebase_core.model.attribute import Attribute
from lorebase_core.model.operation import (
    CreateLinkStep,
    CreateRecordStep,
    DeleteLinkStep,
    DeleteRecordStep,
    Operation,
    OperationStep,
    ReviewMode,
    UpdateRecordStep,
)
from lorebase_core.model.record import Link, Record, format_ref, record_key
from lorebase_core.model.relation import RelationBacking
from lorebase_core.model.schema import Schema
from lorebase_core.types import normalize_key
from lorebase_core.validate.changes import (
    DataChange,
    LinkDelete,
    LinkUpsert,
    RecordDelete,
    RecordUpsert,
)

#: Names Operation expressions define themselves; parameters cannot take them.
RESERVED_PARAMETER_NAMES = frozenset({PARAMS, PRINCIPAL, STEP_VARIABLE})


@dataclass(frozen=True, eq=False)
class CompiledStep:
    """One step with its compiled expressions (``values`` keyed by Attribute id)."""

    index: int
    step: OperationStep
    environment: Environment
    values: Mapping[str, CompiledExpression] = field(default_factory=dict)
    target: CompiledExpression | None = None
    from_: CompiledExpression | None = None
    to: CompiledExpression | None = None


@dataclass(frozen=True, eq=False)
class CompiledOperation:
    schema: Schema
    operation: Operation
    preconditions: tuple[CompiledExpression, ...]
    review_policy: CompiledExpression | None
    steps: tuple[CompiledStep, ...]


@dataclass(frozen=True)
class PreconditionFailure:
    """A precondition that is false (``error`` is ``None``) or could not be evaluated."""

    index: int
    message: str
    error: ExprError | None = None


@dataclass
class StepPlan:
    """The data changes of an Operation's steps and the Record each step touched."""

    changes: list[DataChange] = field(default_factory=list)
    records: list[Ref | None] = field(default_factory=list)


class _Builder:
    """Compiles every expression of an Operation, raising or collecting errors."""

    def __init__(self, schema: Schema, limits: ExprLimits, *, collect: bool) -> None:
        self.schema = schema
        self.limits = limits
        self.errors: list[ExprError] | None = [] if collect else None

    def fail(self, error: ExprError, path: tuple[PathPart, ...]) -> None:
        error.located(path=path)
        if self.errors is None:
            raise error
        self.errors.append(error)

    def unknown(self, message: str, path: tuple[PathPart, ...]) -> None:
        """A dangling reference: schema validation reports it, compiling for use refuses it."""
        if self.errors is None:
            raise ExprError(ExprErrorCode.TYPE, message, path=path)

    def expression(
        self,
        path: tuple[PathPart, ...],
        source: str,
        environment: Environment,
        expected: ExprType,
        *,
        nullable: bool,
    ) -> CompiledExpression | None:
        try:
            return compile(
                source, environment, expected=expected, nullable=nullable, limits=self.limits
            )
        except ExprError as error:
            self.fail(error, path)
            return None

    def values(
        self,
        path: tuple[PathPart, ...],
        values: Mapping[str, str],
        attributes: Mapping[str, Attribute],
        environment: Environment,
    ) -> dict[str, CompiledExpression]:
        compiled: dict[str, CompiledExpression] = {}
        for attribute_id, source in values.items():
            attribute = attributes.get(attribute_id)
            if attribute is None:
                continue  # reported by schema validation
            program = self.expression(
                (*path, "values", attribute_id),
                source,
                environment,
                from_type_spec(attribute.type),
                nullable=True,
            )
            if program is not None:
                compiled[attribute_id] = program
        return compiled

    def operation(self, operation: Operation) -> CompiledOperation:
        schema = self.schema
        for index, parameter in enumerate(operation.parameters):
            if parameter.name in RESERVED_PARAMETER_NAMES:
                self.fail(
                    ExprError(
                        ExprErrorCode.TYPE,
                        f"parameter name {parameter.name!r} is reserved in expressions",
                    ),
                    ("parameters", index, "name"),
                )
        environment = operation_environment(schema, operation)
        preconditions = []
        for index, precondition in enumerate(operation.preconditions):
            program = self.expression(
                ("preconditions", index, "expression"),
                precondition.expression,
                environment,
                BOOL,
                nullable=False,
            )
            if program is not None:
                preconditions.append(program)
        review = None
        if operation.review_policy.mode is ReviewMode.EXPRESSION:
            assert operation.review_policy.expression is not None
            review = self.expression(
                ("review_policy", "expression"),
                operation.review_policy.expression,
                environment,
                BOOL,
                nullable=False,
            )
        steps = tuple(self.step(operation, index) for index in range(len(operation.steps)))
        return CompiledOperation(schema, operation, tuple(preconditions), review, steps)

    def step(self, operation: Operation, index: int) -> CompiledStep:
        step = operation.steps[index]
        path: tuple[PathPart, ...] = ("steps", index)
        environment = operation_environment(self.schema, operation, step_index=index)
        if isinstance(step, CreateLinkStep | DeleteLinkStep):
            relation = self.schema.relations.get(step.relation)
            if relation is None or relation.backing is not RelationBacking.EXPLICIT:
                self.unknown(f"Relation {step.relation} cannot be written", (*path, "relation"))
                return CompiledStep(index, step, environment)
            from_ = self.expression(
                (*path, "from"), step.from_, environment, ref_of(relation.from_kind), nullable=False
            )
            to = self.expression(
                (*path, "to"), step.to, environment, ref_of(relation.to_kind), nullable=False
            )
            values = {}
            if isinstance(step, CreateLinkStep):
                values = self.values(path, step.values, relation.attributes, environment)
            return CompiledStep(index, step, environment, values, from_=from_, to=to)
        kind = self.schema.kinds.get(step.kind)
        if kind is None:
            self.unknown(f"unknown Kind {step.kind}", (*path, "kind"))
            return CompiledStep(index, step, environment)
        target = None
        if isinstance(step, UpdateRecordStep | DeleteRecordStep):
            target = self.expression(
                (*path, "target"), step.target, environment, ref_of(kind.id), nullable=False
            )
        values = {}
        if isinstance(step, CreateRecordStep | UpdateRecordStep):
            values = self.values(path, step.values, kind.attributes, environment)
        return CompiledStep(index, step, environment, values, target=target)


def compile_operation(
    schema: Schema, operation: Operation, limits: ExprLimits | None = None
) -> CompiledOperation:
    """Compile every expression of ``operation``; raises the first ``ExprError`` (with path)."""
    return _Builder(schema, limits or ExprLimits(), collect=False).operation(operation)


def operation_expression_errors(
    schema: Schema, operation: Operation, limits: ExprLimits | None = None
) -> list[ExprError]:
    """Every expression problem of ``operation``; each error's ``path`` is inside the element."""
    builder = _Builder(schema, limits or ExprLimits(), collect=True)
    builder.operation(operation)
    assert builder.errors is not None
    return builder.errors


# Evaluation --------------------------------------------------------------------------------------


def _evaluate(
    compiled: CompiledExpression,
    context: EvaluationContext,
    variables: Mapping[str, object],
    *,
    overlay: Mapping[tuple[str, str], Mapping[str, JsonValue] | None] | None = None,
) -> object:
    return run(
        compiled,
        variables,
        now=context.now,
        resolver=context.resolver,
        limits=context.limits,
        overlay=overlay,
    )


def evaluate_precondition(
    compiled: CompiledExpression,
    record: Record | None,
    parameters: Mapping[str, JsonValue],
    context: EvaluationContext,
) -> bool:
    """Value of a precondition (or any bool expression of an Operation environment).

    ``record`` is the subject Record when the environment has one (``None`` for Operation
    preconditions); ``parameters`` are canonical values by name.
    """
    variables = activation(
        compiled.environment, record=record, parameters=parameters, principal=context.principal
    )
    return require_bool(_evaluate(compiled, context, variables), "precondition")


def check_preconditions(
    compiled: CompiledOperation, parameters: Mapping[str, JsonValue], context: EvaluationContext
) -> list[PreconditionFailure]:
    """Preconditions that are false or fail to evaluate, in declaration order.

    ``RecordNotLoaded`` from the resolver propagates so that the caller can load and retry.
    """
    failures: list[PreconditionFailure] = []
    for index, (program, precondition) in enumerate(
        zip(compiled.preconditions, compiled.operation.preconditions, strict=True)
    ):
        try:
            if not evaluate_precondition(program, None, parameters, context):
                failures.append(PreconditionFailure(index, precondition.message))
        except ExprError as error:
            error.located(path=("preconditions", index, "expression"))
            failures.append(PreconditionFailure(index, precondition.message, error))
    return failures


def evaluate_review_policy(
    compiled: CompiledOperation, parameters: Mapping[str, JsonValue], context: EvaluationContext
) -> bool:
    """Whether the edits go to a Proposal: ``always``, ``never`` or the policy expression."""
    mode = compiled.operation.review_policy.mode
    if mode is ReviewMode.ALWAYS:
        return True
    if compiled.review_policy is None:
        return False
    try:
        return evaluate_precondition(compiled.review_policy, None, parameters, context)
    except ExprError as error:
        raise error.located(path=("review_policy", "expression")) from None


def plan_steps(
    compiled: CompiledOperation, parameters: Mapping[str, JsonValue], context: EvaluationContext
) -> StepPlan:
    """Data changes of the Operation's steps.

    Raises ``ExprError`` (with ``path`` to the failing step expression) when a value cannot
    be computed or does not fit its Attribute, when an updated Record does not exist
    (``record.not_found``) or a created one does (``record.exists``); lets
    ``RecordNotLoaded`` through.
    """
    plan = StepPlan()
    overlay: dict[tuple[str, str], Mapping[str, JsonValue] | None] = {}
    for compiled_step in compiled.steps:
        variables = activation(
            compiled_step.environment,
            parameters=parameters,
            principal=context.principal,
            steps=plan.records,
        )
        planner = _StepPlanner(compiled, compiled_step, context, variables, overlay)
        plan.records.append(planner.run(plan.changes))
    return plan


class _StepPlanner:
    def __init__(
        self,
        compiled: CompiledOperation,
        step: CompiledStep,
        context: EvaluationContext,
        variables: Mapping[str, object],
        overlay: dict[tuple[str, str], Mapping[str, JsonValue] | None],
    ) -> None:
        self.schema = compiled.schema
        self.step = step
        self.context = context
        self.variables = variables
        self.overlay = overlay
        self.path: tuple[PathPart, ...] = ("steps", step.index)

    def value(self, program: CompiledExpression | None, *where: PathPart) -> object:
        if program is None:
            raise evaluation_error("the step expression did not compile").located(
                path=(*self.path, *where)
            )
        try:
            return _evaluate(program, self.context, self.variables, overlay=self.overlay)
        except ExprError as error:
            raise error.located(path=(*self.path, *where)) from None

    def ref(self, program: CompiledExpression | None, kind_id: str, where: str) -> Ref:
        value = self.value(program, where)
        try:
            if isinstance(value, str):
                return Ref(kind_id, normalize_key(value))
            ref = ref_key(value, where)
        except ExprError as error:
            raise error.located(path=(*self.path, where)) from None
        if ref.kind != kind_id:
            raise ExprError(
                ExprErrorCode.VALUE_INVALID,
                f"{where} refers to a {self.kind_name(ref.kind)} Record, expected "
                f"{self.kind_name(kind_id)}",
                path=(*self.path, where),
            )
        return ref

    def kind_name(self, kind_id: str) -> str:
        kind = self.schema.kinds.get(kind_id)
        return kind.api_name if kind is not None else kind_id

    def encoded(self, attributes: Mapping[str, Attribute]) -> dict[str, JsonValue]:
        values: dict[str, JsonValue] = {}
        for attribute_id, program in self.step.values.items():
            value = self.value(program, "values", attribute_id)
            try:
                values[attribute_id] = encode_result(attributes[attribute_id].type, value)
            except ExprError as error:
                error.message = f"{attributes[attribute_id].api_name}: {error.message}"
                raise error.located(path=(*self.path, "values", attribute_id)) from None
        return values

    def current(self, kind_id: str, key: str) -> Mapping[str, JsonValue] | None:
        if (kind_id, key) in self.overlay:
            return self.overlay[(kind_id, key)]
        if self.context.resolver is None:
            return None
        return self.context.resolver.record(kind_id, key)

    def run(self, changes: list[DataChange]) -> Ref | None:
        step = self.step.step
        if isinstance(step, CreateLinkStep | DeleteLinkStep):
            relation = self.schema.relations[step.relation]
            from_ref = self.ref(self.step.from_, relation.from_kind, "from")
            to_ref = self.ref(self.step.to, relation.to_kind, "to")
            if isinstance(step, DeleteLinkStep):
                changes.append(
                    LinkDelete(relation=relation.id, from_key=from_ref.key, to_key=to_ref.key)
                )
                return None
            link = Link(
                relation=relation.id,
                from_key=from_ref.key,
                to_key=to_ref.key,
                values=self.encoded(relation.attributes),
            )
            changes.append(LinkUpsert(link=link))
            return None
        kind = self.schema.kinds[step.kind]
        if isinstance(step, CreateRecordStep):
            values = self.encoded(kind.attributes)
            key = record_key(kind, values) if kind.has_natural_key else None
            if key is None:
                key = self.mint(kind.has_natural_key)
            elif self.current(kind.id, key) is not None:
                raise ExprError(
                    ExprErrorCode.RECORD_EXISTS,
                    f"Record {format_ref(kind.api_name, key)} exists already",
                    path=self.path,
                )
            changes.append(RecordUpsert(record=Record(kind=kind.id, key=key, values=values)))
            self.overlay[(kind.id, key)] = values
            return Ref(kind.id, key)
        target = self.ref(self.step.target, kind.id, "target")
        if isinstance(step, DeleteRecordStep):
            changes.append(RecordDelete(kind=kind.id, key=target.key))
            self.overlay[(kind.id, target.key)] = None
            return target
        current = self.current(kind.id, target.key)
        if current is None:
            raise ExprError(
                ExprErrorCode.RECORD_NOT_FOUND,
                f"Record {format_ref(kind.api_name, target.key)} does not exist",
                path=(*self.path, "target"),
            )
        merged: dict[str, JsonValue] = dict(current)
        for attribute_id, value in self.encoded(kind.attributes).items():
            if value is None:
                merged.pop(attribute_id, None)
            else:
                merged[attribute_id] = value
        changes.append(RecordUpsert(record=Record(kind=kind.id, key=target.key, values=merged)))
        self.overlay[(kind.id, target.key)] = merged
        return target

    def mint(self, natural: bool) -> str:
        if natural:
            raise ExprError(
                ExprErrorCode.VALUE_INVALID,
                "a primary key value of the created Record is missing",
                path=self.path,
            )
        mint: Callable[[], str] | None = self.context.mint_key
        if mint is None:
            raise ExprError(
                ExprErrorCode.EVALUATION,
                "the Kind has no natural key and the caller supplied no key minting",
                path=self.path,
            )
        return mint()
