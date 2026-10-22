"""Expressions over one row of a Source (design 5.9): the ``expression`` of an Attribute
mapping.

A row is what the Source reads before any mapping: column name to raw value (text for CSV and
TSV, native values for JSON and databases). Every column whose name is an identifier is a
variable, typed ``dyn`` because the Source does not declare column types; ``row`` is the map of
all columns, for names that are not identifiers (``row["Legal name"]``). Nothing else is in
scope: no Records, no Links, no principal.

The result is a runtime value that the mapping's transforms take as the column value, and that
is coerced to the Attribute type like one. ``row_value`` gives its JSON form.

``RowExpressions`` compiles each expression once over the columns of a Source (the header, or
every key its rows have) and evaluates it on any row: a column the row lacks is null. Scribe
maps rows and Keeper previews them with it, so both give the same value for the same rows.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal

from pydantic import JsonValue

from lorebase_core.expr.compiler import CompiledExpression, compile, run
from lorebase_core.expr.environment import Environment
from lorebase_core.expr.errors import ExprError
from lorebase_core.expr.limits import ExprLimits
from lorebase_core.expr.types import DYN, STRING, map_of
from lorebase_core.expr.values import to_raw
from lorebase_core.model.schema import Schema

#: The variable holding every column of the row.
ROW = "row"

#: Decimal results whose adjusted exponent is beyond this get the exponent form in
#: ``row_value`` (``1E+100000``), not a plain form of that many digits.
PLAIN_DECIMAL_MAX_EXPONENT = 100

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
    """The runtime value of ``compiled`` for ``row``; raises ``ExprError``. Every column
    ``compiled`` was compiled over is a variable: one the row lacks is null."""
    variables: dict[str, object] = {
        name: row.get(name) for name in compiled.environment.variables if name != ROW
    }
    variables[ROW] = dict(row)
    return run(compiled, variables, now=now)


class RowExpressions:
    """The expressions of one Source over its columns, each compiled once.

    ``columns`` are the columns the Source has (its header, or every key its rows have); a row
    with a column not declared yet adds it, and the expressions are compiled again over the
    larger set. A declared column a row lacks is null, as a variable and in ``row``.
    """

    def __init__(
        self, schema: Schema, columns: Iterable[str] = (), limits: ExprLimits | None = None
    ) -> None:
        self._schema = schema
        self._limits = limits
        self._columns: dict[str, None] = dict.fromkeys(columns)
        self._programs: dict[str, CompiledExpression | ExprError] = {}

    @property
    def columns(self) -> tuple[str, ...]:
        """The declared columns, in the order they were first seen."""
        return tuple(self._columns)

    def declare(self, columns: Iterable[str]) -> None:
        """Add columns; the expressions compile again only when one is new."""
        added = [c for c in columns if c not in self._columns]
        if added:
            self._columns.update(dict.fromkeys(added))
            self._programs.clear()

    def compiled(self, expression: str) -> CompiledExpression:
        """``expression`` compiled over the declared columns; raises ``ExprError`` (the error
        is kept like a program, so a bad expression is checked once)."""
        program = self._programs.get(expression)
        if program is None:
            try:
                program = compile_row_expression(
                    self._schema, expression, self._columns, self._limits
                )
            except ExprError as error:
                program = error
            self._programs[expression] = program
        if isinstance(program, ExprError):
            raise program.with_traceback(None)
        return program

    def evaluate(self, expression: str, row: Mapping[str, object], *, now: datetime) -> object:
        """The runtime value of ``expression`` for ``row``; raises ``ExprError``."""
        self.declare(row)
        compiled = self.compiled(expression)
        return evaluate_row_expression(compiled, {**dict.fromkeys(self._columns), **row}, now=now)


def row_value(value: object) -> JsonValue:
    """JSON form of a runtime value: decimals and dates as text, containers recursively.
    A decimal is in plain form unless its exponent is beyond ``PLAIN_DECIMAL_MAX_EXPONENT``."""
    raw = to_raw(value)
    if raw is None or isinstance(raw, bool | int | float | str):
        return raw
    if isinstance(raw, Decimal):
        if raw.is_finite() and abs(raw.adjusted()) > PLAIN_DECIMAL_MAX_EXPONENT:
            return str(raw)
        return format(raw, "f")
    if isinstance(raw, datetime | date):
        return raw.isoformat()
    if isinstance(raw, list):
        return [row_value(item) for item in raw]
    if isinstance(raw, dict):
        return {str(k): row_value(v) for k, v in raw.items()}
    return str(raw)
