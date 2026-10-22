"""Expression engine (design 5.7, 5.8, AC-06): CEL parsed by cel-python, type checked against
the Schema and evaluated in a bounded, deterministic sandbox without I/O.

- ``compile(expression, environment)`` parses and type checks; environments come from
  ``operation_environment`` and ``record_environment``.
- ``compile_operation`` / ``compile_derivation`` compile every expression of an element;
  ``derivation_inputs`` and ``links_read`` tell what a Derivation reads.
- ``evaluate_precondition``, ``check_preconditions``, ``evaluate_review_policy`` and
  ``plan_steps`` evaluate Operations; ``evaluate_derivation`` and ``matches_filter``
  evaluate Derivations. Records come from a caller-supplied ``RecordResolver``.
- ``compile_row_expression`` and ``evaluate_row_expression`` handle the ``expression`` of a
  Source's Attribute mapping, over one row of the Source.
- ``ExprLimits`` bounds compiling and evaluation; ``ExprError`` reports problems with a
  stable code, the position in the expression and the path in the schema element.
"""

from lorebase_core.expr.compiler import CompiledExpression, compile, run
from lorebase_core.expr.context import EvaluationContext, Principal, activation
from lorebase_core.expr.derivations import (
    CompiledDerivation,
    compile_derivation,
    derivation_expression_errors,
    derivation_inputs,
    derivation_reads,
    evaluate_derivation,
    links_read,
    matches_filter,
)
from lorebase_core.expr.environment import (
    Environment,
    LinkField,
    operation_environment,
    record_environment,
)
from lorebase_core.expr.errors import ExprError, ExprErrorCode, Position
from lorebase_core.expr.limits import ExprLimits
from lorebase_core.expr.operations import (
    RESERVED_PARAMETER_NAMES,
    CompiledOperation,
    CompiledStep,
    PreconditionFailure,
    StepPlan,
    check_preconditions,
    compile_operation,
    evaluate_precondition,
    evaluate_review_policy,
    operation_expression_errors,
    plan_steps,
)
from lorebase_core.expr.resolver import (
    LinkedRecord,
    LinksRequest,
    MemoryResolver,
    RecordNotLoaded,
    RecordRequest,
    RecordResolver,
)
from lorebase_core.expr.rows import (
    compile_row_expression,
    evaluate_row_expression,
    row_environment,
    row_value,
    row_variables,
)
from lorebase_core.expr.types import ExprType, TypeKind, from_type_spec

__all__ = [
    "RESERVED_PARAMETER_NAMES",
    "CompiledDerivation",
    "CompiledExpression",
    "CompiledOperation",
    "CompiledStep",
    "Environment",
    "EvaluationContext",
    "ExprError",
    "ExprErrorCode",
    "ExprLimits",
    "ExprType",
    "LinkField",
    "LinkedRecord",
    "LinksRequest",
    "MemoryResolver",
    "Position",
    "PreconditionFailure",
    "Principal",
    "RecordNotLoaded",
    "RecordRequest",
    "RecordResolver",
    "StepPlan",
    "TypeKind",
    "activation",
    "check_preconditions",
    "compile",
    "compile_derivation",
    "compile_operation",
    "compile_row_expression",
    "derivation_expression_errors",
    "derivation_inputs",
    "derivation_reads",
    "evaluate_derivation",
    "evaluate_precondition",
    "evaluate_review_policy",
    "evaluate_row_expression",
    "from_type_spec",
    "links_read",
    "matches_filter",
    "operation_environment",
    "operation_expression_errors",
    "plan_steps",
    "record_environment",
    "row_environment",
    "row_value",
    "row_variables",
    "run",
]
