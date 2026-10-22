"""Preview of the expression mappings of a candidate Source (design 5.9) on sample rows.

The candidate is a schema change set naming the Source: the schema it produces is validated
like a schema change, then the `expression` of every Attribute mapping of the Source is
evaluated on each row the caller sends, with the engine Scribe maps rows with
(`lorebase_core.expr.rows`, within `schema.expression_limits`). The answer holds the value of
each expression before the mapping's transforms; transforms and the coercion to the Attribute
type are the mapper's (`lorebase_scribe.sources.mapping`, which Keeper does not import).

The rows are the caller's own input and the answer is made of them: no Record is read, nothing is
stored and no connection is opened, so reading the schema is the only permission involved.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import JsonValue

from lorebase_core.expr import (
    ExprError,
    ExprLimits,
    compile_row_expression,
    evaluate_row_expression,
    row_value,
)
from lorebase_core.expr.compiler import CompiledExpression
from lorebase_core.model import Schema, Source
from lorebase_core.validate import SchemaRules
from lorebase_keeper.services.derivation_preview import CandidateInvalidError
from lorebase_keeper.services.errors import Finding
from lorebase_keeper.services.schema import (
    ChangeSetInput,
    ChangeSetInvalidError,
    static_issues,
    target_schema,
)

SOURCE_NOT_FOUND = "source.not_found"


@dataclass(frozen=True)
class ExpressionCell:
    """What one expression gives for one row: the value, or why there is none."""

    value: JsonValue
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class SourceCandidate:
    """A validated candidate: the schema the change set asks for and the Source in it."""

    schema: Schema
    source: Source


def candidate_of(
    current: Schema, change: ChangeSetInput, name: str, rules: SchemaRules
) -> SourceCandidate:
    """The Source `name` of the schema `change` asks for, once the schema is valid.

    Raises `CandidateInvalidError` when the change set cannot be read, fails static validation
    or `name` is not a Source of it.
    """
    try:
        schema = target_schema(current, change)
    except ChangeSetInvalidError as invalid:
        raise CandidateInvalidError(invalid.issues) from None
    issues = static_issues(schema, rules)
    if issues:
        raise CandidateInvalidError(issues)
    source = schema.source_by_api_name(name)
    if source is None:
        raise CandidateInvalidError(
            [Finding(SOURCE_NOT_FOUND, f"No Source {name!r} in the schema.", ("sources",))]
        )
    return SourceCandidate(schema, source)


def expression_attributes(candidate: SourceCandidate) -> dict[str, str]:
    """Api name of each Attribute mapped by an expression, with the expression."""
    schema, source = candidate.schema, candidate.source
    kind = schema.kinds[source.target_kind]
    return {
        kind.attributes[attribute_id].api_name: mapping.expression
        for attribute_id, mapping in source.attribute_mapping.items()
        if mapping.expression is not None and attribute_id in kind.attributes
    }


def evaluate(
    candidate: SourceCandidate,
    rows: Sequence[Mapping[str, JsonValue]],
    *,
    limits: ExprLimits,
    now: datetime,
) -> list[dict[str, ExpressionCell]]:
    """The value of every expression mapping of the Source for each row, in row order."""
    expressions = expression_attributes(candidate)
    programs: dict[tuple[str, frozenset[str]], CompiledExpression | ExprError] = {}

    def program(expression: str, row: Mapping[str, JsonValue]) -> CompiledExpression | ExprError:
        key = (expression, frozenset(row))
        if key not in programs:
            try:
                programs[key] = compile_row_expression(candidate.schema, expression, row, limits)
            except ExprError as error:
                programs[key] = error
        return programs[key]

    cells: list[dict[str, ExpressionCell]] = []
    for row in rows:
        found: dict[str, ExpressionCell] = {}
        for attribute, expression in expressions.items():
            compiled = program(expression, row)
            try:
                if isinstance(compiled, ExprError):
                    raise compiled
                found[attribute] = ExpressionCell(
                    row_value(evaluate_row_expression(compiled, row, now=now))
                )
            except ExprError as error:
                found[attribute] = ExpressionCell(None, error.code.value, error.describe())
        cells.append(found)
    return cells
