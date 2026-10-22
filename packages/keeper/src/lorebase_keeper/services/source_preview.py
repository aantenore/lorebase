"""Preview of the expression mappings of a candidate Source (design 5.9) on sample rows.

The candidate is a schema change set naming the Source: the schema it produces is validated
like a schema change, then the `expression` of every Attribute mapping of the Source is
evaluated on each row the caller sends, with the engine Scribe maps rows with
(`lorebase_core.expr.rows`, within `schema.expression_limits`). The answer holds the value of
each expression before the mapping's transforms; transforms and the coercion to the Attribute
type are the mapper's (`lorebase_scribe.sources.mapping`, which Keeper does not import).

The rows are the caller's own input and the answer is made of them: no Record is read, nothing is
stored and no connection is opened, so reading the schema is the only permission involved (a
grant narrowed to a Kind is enough when it names the Source's target Kind). Expressions are
compiled once over every column the rows have; a column a row lacks is null, as in Scribe.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import JsonValue

from lorebase_core.expr import ExprError, ExprLimits, RowExpressions, row_value
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
    current: Schema,
    change: ChangeSetInput,
    name: str,
    rules: SchemaRules,
    *,
    authorize: Callable[[Schema, Source | None], None] | None = None,
) -> SourceCandidate:
    """The Source `name` of the schema `change` asks for, once the schema is valid.

    `authorize` is called with the schema the change set produces and the Source in it (None
    when there is none) before the schema is validated, so a caller who may not read the
    Source's target Kind is refused before any issue is reported. Raises
    `CandidateInvalidError` when the change set cannot be read, fails static validation or
    `name` is not a Source of it.
    """
    try:
        schema = target_schema(current, change)
    except ChangeSetInvalidError as invalid:
        raise CandidateInvalidError(invalid.issues) from None
    source = schema.source_by_api_name(name)
    if authorize is not None:
        authorize(schema, source)
    issues = static_issues(schema, rules)
    if issues:
        raise CandidateInvalidError(issues)
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
    """The value of every expression mapping of the Source for each row, in row order.

    Expressions are compiled once over every column the rows have, as Scribe compiles them
    over the columns of the Source: a column a row lacks is null."""
    expressions = expression_attributes(candidate)
    columns: dict[str, None] = {}
    for row in rows:
        columns.update(dict.fromkeys(row))
    programs = RowExpressions(candidate.schema, columns, limits)
    cells: list[dict[str, ExpressionCell]] = []
    for row in rows:
        found: dict[str, ExpressionCell] = {}
        for attribute, expression in expressions.items():
            try:
                found[attribute] = ExpressionCell(
                    row_value(programs.evaluate(expression, row, now=now))
                )
            except ExprError as error:
                found[attribute] = ExpressionCell(None, error.code.value, error.describe())
        cells.append(found)
    return cells
