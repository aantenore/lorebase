"""Expressions over one row of a Source (design 5.9): the ``expression`` of an Attribute
mapping.

A row is what the Source reads before any mapping: column name to raw value (text for CSV and
TSV, native values for JSON and databases). Every column whose name is an identifier is a
variable, typed ``dyn`` because the Source does not declare column types; ``row`` is the map of
all columns, for names that are not identifiers (``row["Legal name"]``). Nothing else is in
scope: no Records, no Links, no principal.

The result is a runtime value that the mapping's transforms take as the column value, and that
is coerced to the Attribute type like one. ``row_value`` gives its JSON form.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal

from pydantic import JsonValue

from lorebase_core.expr.compiler import CompiledExpression, compile, run
from lorebase_core.expr.environment import Environment
from lorebase_core.expr.limits import ExprLimits
from lorebase_core.expr.types import DYN, STRING, map_of
from lorebase_core.expr.values import to_raw
from lorebase_core.model.schema import Schema

#: The variable holding every column of the row.
ROW = "row"

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED = frozenset(
    {"true", "false", "null", "in", "as", "break", "const", "continue", "else", "for", "function"}
    | {"if", "import", "let", "loop", "package", "namespace", "return", "var", "void", "while"}
    | {ROW}
)


def row_variables(columns: Iterable[str]) -> list[str]:
    """The columns that are variables of a row expression, sorted."""
    return sorted({c for c in columns if _IDENTIFIER.match(c) and c not in _RESERVED})


def row_environment(schema: Schema, columns: Iterable[str]) -> Environment:
    """Environment of the expressions over rows with these columns."""
    variables = dict.fromkeys(row_variables(columns), DYN)
    variables[ROW] = map_of(STRING, DYN)
    return Environment(schema=schema, variables=variables)


def compile_row_expression(
    schema: Schema, expression: str, columns: Iterable[str], limits: ExprLimits | None = None
) -> CompiledExpression:
    """Parse and type check ``expression`` over rows with ``columns``; raises ``ExprError``."""
    return compile(expression, row_environment(schema, columns), limits=limits)


def evaluate_row_expression(
    compiled: CompiledExpression, row: Mapping[str, object], *, now: datetime
) -> object:
    """The runtime value of ``compiled`` for ``row``; raises ``ExprError``."""
    variables: dict[str, object] = {name: row.get(name) for name in row_variables(row)}
    variables[ROW] = dict(row)
    return run(compiled, variables, now=now)


def row_value(value: object) -> JsonValue:
    """JSON form of a runtime value: decimals and dates as text, containers recursively."""
    raw = to_raw(value)
    if raw is None or isinstance(raw, bool | int | float | str):
        return raw
    if isinstance(raw, Decimal):
        return format(raw, "f")
    if isinstance(raw, datetime | date):
        return raw.isoformat()
    if isinstance(raw, list):
        return [row_value(item) for item in raw]
    if isinstance(raw, dict):
        return {str(k): row_value(v) for k, v in raw.items()}
    return str(raw)
