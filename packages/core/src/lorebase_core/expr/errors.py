"""Errors of the expression engine: one exception type with a stable code and a position."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

PathPart = str | int


class ExprErrorCode(StrEnum):
    """Stable codes; ``syntax`` and ``type`` are static, the others come from evaluation."""

    SYNTAX = "expression.syntax"
    TYPE = "expression.type"
    EVALUATION = "expression.evaluation"
    LIMIT_EXCEEDED = "expression.limit_exceeded"
    VALUE_INVALID = "expression.value_invalid"
    RECORD_NOT_FOUND = "record.not_found"
    RECORD_EXISTS = "record.exists"


@dataclass(frozen=True, slots=True)
class Position:
    """1-based line and column of a node in the expression source."""

    line: int
    column: int

    def __str__(self) -> str:
        return f"{self.line}:{self.column}"


class ExprError(Exception):
    """A problem with an expression: compiling it, or evaluating it.

    ``path`` locates the expression inside its schema element (for example
    ``("preconditions", 0, "expression")``); it is empty until a caller sets it.
    """

    def __init__(
        self,
        code: ExprErrorCode,
        message: str,
        *,
        position: Position | None = None,
        source: str | None = None,
        path: tuple[PathPart, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.position = position
        self.source = source
        self.path = path

    def located(
        self, *, source: str | None = None, path: tuple[PathPart, ...] | None = None
    ) -> ExprError:
        """This error with the expression source and path filled in where they are missing."""
        if source is not None and self.source is None:
            self.source = source
        if path is not None and not self.path:
            self.path = path
        return self

    def describe(self) -> str:
        """``line:column: message`` when the position is known, the message otherwise."""
        if self.position is None:
            return self.message
        return f"{self.position}: {self.message}"

    def snippet(self) -> str:
        """The source line of the error with a caret under the column (empty if unknown)."""
        if self.position is None or self.source is None:
            return ""
        lines = self.source.splitlines() or [""]
        line = lines[min(self.position.line, len(lines)) - 1]
        return f"{line}\n{' ' * (self.position.column - 1)}^"

    def __str__(self) -> str:
        return self.describe()


def syntax_error(message: str, position: Position | None = None) -> ExprError:
    return ExprError(ExprErrorCode.SYNTAX, message, position=position)


def type_error(message: str, position: Position | None = None) -> ExprError:
    return ExprError(ExprErrorCode.TYPE, message, position=position)


def evaluation_error(message: str, position: Position | None = None) -> ExprError:
    return ExprError(ExprErrorCode.EVALUATION, message, position=position)


def limit_error(message: str, position: Position | None = None) -> ExprError:
    return ExprError(ExprErrorCode.LIMIT_EXCEEDED, message, position=position)
