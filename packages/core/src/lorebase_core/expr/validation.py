"""Static validation of every expression of a Schema (parse and type check).

Used by ``validate_schema`` when expressions are enabled; each problem is a
``schema.expression_invalid`` issue whose path points at the expression and whose details
carry the source, line and column.
"""

from __future__ import annotations

from pydantic import JsonValue

from lorebase_core.expr.derivations import derivation_expression_errors
from lorebase_core.expr.errors import ExprError, PathPart
from lorebase_core.expr.limits import ExprLimits
from lorebase_core.expr.operations import operation_expression_errors
from lorebase_core.model.schema import Schema
from lorebase_core.validate.issues import Issue, IssueCode


def _issue(base: tuple[PathPart, ...], error: ExprError, *, kind: str | None = None) -> Issue:
    details: dict[str, JsonValue] = {"error": error.code.value}
    if error.source is not None:
        details["expression"] = error.source
    if error.position is not None:
        details["line"] = error.position.line
        details["column"] = error.position.column
    return Issue(
        code=IssueCode.SCHEMA_EXPRESSION_INVALID,
        path=(*base, *error.path),
        message=f"expression: {error.describe()}",
        kind=kind,
        details=details,
    )


def schema_expression_issues(schema: Schema, limits: ExprLimits | None = None) -> list[Issue]:
    """``schema.expression_invalid`` issues of every Operation and Derivation expression."""
    issues: list[Issue] = []
    for operation in schema.operations.values():
        base: tuple[PathPart, ...] = ("operations", operation.id)
        issues.extend(
            _issue(base, error) for error in operation_expression_errors(schema, operation, limits)
        )
    for derivation in schema.derivations.values():
        base = ("derivations", derivation.id)
        target = derivation.target
        issues.extend(
            _issue(base, error, kind=target[0] if target else None)
            for error in derivation_expression_errors(schema, derivation, limits)
        )
    return issues
