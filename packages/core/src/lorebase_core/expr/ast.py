"""Expression syntax: parse CEL source with cel-python and build a small typed-independent AST.

cel-python provides the grammar (the CEL specification's, as a Lark LALR parser) with
positions; the engine converts its parse tree into the nodes below so that checking and
evaluation do not depend on the parser's internals.

Supported syntax: literals (int, double, string, bytes, bool, null), identifiers, field
selection, indexing, global and method calls, list and map literals, unary ``-`` and ``!``,
arithmetic, comparisons, ``in``, ``&&``, ``||``, ``?:`` and the macros ``has``, ``all``,
``exists``, ``exists_one``, ``map`` (two arguments), ``filter`` and ``count``. Unsigned
integers and message construction (``Type{field: value}``) are not supported.

``$step[n]`` in Operation steps refers to the Record of an earlier step. ``$`` is not CEL
syntax, so it is rewritten to the reserved identifier ``_step`` before parsing (same length,
so positions are unchanged); text inside string literals is left alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from celpy.celparser import CELParser
from celpy.evaluation import celbytes, celstr
from lark import Lark, Token, Tree
from lark.exceptions import (
    LarkError,
    UnexpectedCharacters,
    UnexpectedEOF,
    UnexpectedInput,
    UnexpectedToken,
)

from lorebase_core.expr.errors import ExprError, Position, limit_error, syntax_error
from lorebase_core.expr.limits import ExprLimits
from lorebase_core.expr.types import (
    BOOL,
    BYTES,
    DOUBLE,
    INT,
    NULL,
    ExprType,
    string_values,
)
from lorebase_core.types import INT64_MAX, INT64_MIN

#: Source form and identifier of step references (see the module docstring).
STEP_SYNTAX = "$step"
STEP_VARIABLE = "_step"

#: Comprehension macros: ``target.macro(variable, body)``.
MACROS = frozenset({"all", "exists", "exists_one", "map", "filter", "count"})

_PASS_THROUGH = frozenset(
    {
        "expr",
        "conditionalor",
        "conditionaland",
        "relation",
        "addition",
        "multiplication",
        "unary",
        "member",
        "primary",
        "paren_expr",
    }
)
_RELATIONS = {
    "relation_lt": "<",
    "relation_le": "<=",
    "relation_gt": ">",
    "relation_ge": ">=",
    "relation_eq": "==",
    "relation_ne": "!=",
    "relation_in": "in",
}
_ARITHMETIC = {
    "addition_add": "+",
    "addition_sub": "-",
    "multiplication_mul": "*",
    "multiplication_div": "/",
    "multiplication_mod": "%",
}


@dataclass(frozen=True, eq=False, slots=True)
class Node:
    pos: Position


@dataclass(frozen=True, eq=False, slots=True)
class Literal(Node):
    value: object
    type: ExprType


@dataclass(frozen=True, eq=False, slots=True)
class Ident(Node):
    name: str


@dataclass(frozen=True, eq=False, slots=True)
class Select(Node):
    """``operand.field``; with ``test`` it is ``has(operand.field)``."""

    operand: Node
    field: str
    test: bool = False


@dataclass(frozen=True, eq=False, slots=True)
class Index(Node):
    operand: Node
    index: Node


@dataclass(frozen=True, eq=False, slots=True)
class Call(Node):
    """``function(args)``, or ``target.function(args)`` when ``target`` is set."""

    function: str
    target: Node | None
    args: tuple[Node, ...]


@dataclass(frozen=True, eq=False, slots=True)
class Comprehension(Node):
    """``target.macro(variable, body)``."""

    macro: str
    target: Node
    variable: str
    body: Node


@dataclass(frozen=True, eq=False, slots=True)
class ListLit(Node):
    items: tuple[Node, ...]


@dataclass(frozen=True, eq=False, slots=True)
class MapLit(Node):
    entries: tuple[tuple[Node, Node], ...]


@dataclass(frozen=True, eq=False, slots=True)
class Unary(Node):
    op: str
    operand: Node


@dataclass(frozen=True, eq=False, slots=True)
class Binary(Node):
    op: str
    left: Node
    right: Node


@dataclass(frozen=True, eq=False, slots=True)
class Conditional(Node):
    condition: Node
    then: Node
    otherwise: Node


def rewrite_step_references(source: str) -> str:
    """Replace ``$step`` outside string literals by ``_step`` (see the module docstring)."""
    out: list[str] = []
    index = 0
    quote: str | None = None
    length = len(source)
    while index < length:
        char = source[index]
        if quote is not None:
            if char == "\\":
                out.append(source[index : index + 2])
                index += 2
                continue
            if source.startswith(quote, index):
                out.append(quote)
                index += len(quote)
                quote = None
                continue
            out.append(char)
            index += 1
            continue
        if char in "\"'":
            quote = char * 3 if source.startswith(char * 3, index) else char
            out.append(quote)
            index += len(quote)
            continue
        if source.startswith(STEP_SYNTAX, index):
            out.append(STEP_VARIABLE)
            index += len(STEP_SYNTAX)
            continue
        out.append(char)
        index += 1
    return "".join(out)


def parse(source: str, limits: ExprLimits) -> Node:
    """Parse ``source`` into an AST. Raises ``ExprError`` (``syntax`` or ``limit_exceeded``)."""
    if len(source) > limits.max_source_length:
        raise limit_error(
            f"expression is longer than {limits.max_source_length} characters"
        ).located(source=source)
    text = rewrite_step_references(source)
    if not text.strip():
        raise syntax_error("expression is empty").located(source=source)
    try:
        tree = _lark().parse(text)
    except UnexpectedInput as error:
        message, position = _syntax_problem(text, error)
        raise syntax_error(message, position).located(source=source) from None
    except LarkError as error:
        raise syntax_error(f"syntax error: {error}").located(source=source) from None
    except RecursionError:
        raise limit_error(f"expression nests deeper than {limits.max_depth} levels").located(
            source=source
        ) from None
    try:
        return _Builder(limits.max_depth).build(tree, 0, Position(1, 1))
    except ExprError as error:
        raise error.located(source=source) from None


def _lark() -> Lark:
    """cel-python's CEL grammar (loaded once by its ``CELParser``); used directly so that
    parse errors keep the offending token and parsing holds no per-call state."""
    CELParser()
    parser = CELParser.CEL_PARSER
    assert parser is not None
    return parser


def _end(text: str) -> Position:
    lines = text.splitlines() or [""]
    return Position(len(lines), len(lines[-1]) + 1)


def _syntax_problem(text: str, error: UnexpectedInput) -> tuple[str, Position]:
    if isinstance(error, UnexpectedEOF) or (
        isinstance(error, UnexpectedToken) and error.token.type == "$END"
    ):
        return "syntax error: unexpected end of expression", _end(text)
    position = Position(error.line, error.column)
    if isinstance(error, UnexpectedCharacters):
        found = error.char
    elif isinstance(error, UnexpectedToken):
        found = str(error.token.value)
    else:
        found = text.splitlines()[error.line - 1][error.column - 1 :][:1]
    if found == "$":
        return f"syntax error: unexpected '$' ({STEP_SYNTAX}[n] is the only $ form)", position
    return f"syntax error: unexpected {found!r}", position


class _Builder:
    """Converts the cel-python parse tree into AST nodes, bounding the nesting depth."""

    def __init__(self, max_depth: int) -> None:
        self.max_depth = max_depth

    def build(self, item: Tree[Token] | Token, depth: int, parent: Position) -> Node:
        if isinstance(item, Token):
            raise syntax_error(f"unexpected token {item.value!r}", _token_position(item, parent))
        tree = item
        while (
            tree.data in _PASS_THROUGH
            and len(tree.children) == 1
            and isinstance(tree.children[0], Tree)
        ):
            tree = tree.children[0]
        position = _tree_position(tree, parent)
        if depth >= self.max_depth:
            raise limit_error(f"expression nests deeper than {self.max_depth} levels", position)
        handler = getattr(self, f"_{tree.data}", None)
        if handler is None:
            raise syntax_error(f"unsupported syntax ({tree.data})", position)
        node: Node = handler(tree, depth + 1, position)
        return node

    def _expr(self, tree: Tree[Token], depth: int, pos: Position) -> Node:
        condition, then, otherwise = tree.children
        return Conditional(
            pos,
            self.build(condition, depth, pos),
            self.build(then, depth, pos),
            self.build(otherwise, depth, pos),
        )

    def _conditionalor(self, tree: Tree[Token], depth: int, pos: Position) -> Node:
        left, right = tree.children
        return Binary(pos, "||", self.build(left, depth, pos), self.build(right, depth, pos))

    def _conditionaland(self, tree: Tree[Token], depth: int, pos: Position) -> Node:
        left, right = tree.children
        return Binary(pos, "&&", self.build(left, depth, pos), self.build(right, depth, pos))

    def _operator(
        self, tree: Tree[Token], depth: int, pos: Position, table: dict[str, str]
    ) -> Node:
        operator, right = tree.children
        assert isinstance(operator, Tree)
        (left,) = operator.children
        op = table[operator.data]
        right_node = self.build(right, depth, pos)
        return Binary(_tree_position(operator, pos), op, self.build(left, depth, pos), right_node)

    def _relation(self, tree: Tree[Token], depth: int, pos: Position) -> Node:
        return self._operator(tree, depth, pos, _RELATIONS)

    def _addition(self, tree: Tree[Token], depth: int, pos: Position) -> Node:
        return self._operator(tree, depth, pos, _ARITHMETIC)

    def _multiplication(self, tree: Tree[Token], depth: int, pos: Position) -> Node:
        return self._operator(tree, depth, pos, _ARITHMETIC)

    def _unary(self, tree: Tree[Token], depth: int, pos: Position) -> Node:
        operator, operand = tree.children
        assert isinstance(operator, Tree)
        op = "!" if operator.data == "unary_not" else "-"
        return Unary(pos, op, self.build(operand, depth, pos))

    def _member_dot(self, tree: Tree[Token], depth: int, pos: Position) -> Node:
        operand, name = tree.children
        assert isinstance(name, Token)
        return Select(_token_position(name, pos), self.build(operand, depth, pos), str(name.value))

    def _member_dot_arg(self, tree: Tree[Token], depth: int, pos: Position) -> Node:
        operand, name, *rest = tree.children
        assert isinstance(name, Token)
        function = str(name.value)
        name_pos = _token_position(name, pos)
        target = self.build(operand, depth, pos)
        args = self._arguments(rest, depth, pos)
        if function in MACROS:
            if len(args) != 2 or not isinstance(args[0], Ident):
                raise syntax_error(
                    f"{function}() takes a variable name and an expression, "
                    f"for example list.{function}(x, x > 0)",
                    name_pos,
                )
            return Comprehension(name_pos, function, target, args[0].name, args[1])
        return Call(name_pos, function, target, args)

    def _member_index(self, tree: Tree[Token], depth: int, pos: Position) -> Node:
        operand, index = tree.children
        return Index(pos, self.build(operand, depth, pos), self.build(index, depth, pos))

    def _member_object(self, tree: Tree[Token], depth: int, pos: Position) -> Node:
        raise syntax_error("message construction (Type{field: value}) is not supported", pos)

    def _dot_ident(self, tree: Tree[Token], depth: int, pos: Position) -> Node:
        return self._ident(tree, depth, pos)

    def _dot_ident_arg(self, tree: Tree[Token], depth: int, pos: Position) -> Node:
        return self._ident_arg(tree, depth, pos)

    def _ident(self, tree: Tree[Token], depth: int, pos: Position) -> Node:
        (name,) = tree.children
        assert isinstance(name, Token)
        return Ident(_token_position(name, pos), str(name.value))

    def _ident_arg(self, tree: Tree[Token], depth: int, pos: Position) -> Node:
        name, *rest = tree.children
        assert isinstance(name, Token)
        function = str(name.value)
        args = self._arguments(rest, depth, pos)
        if function == "has":
            if len(args) != 1 or not isinstance(args[0], Select) or args[0].test:
                raise syntax_error("has() takes one field selection, for example has(x.f)", pos)
            selection = args[0]
            return Select(selection.pos, selection.operand, selection.field, test=True)
        return Call(pos, function, None, args)

    def _arguments(
        self, rest: list[Tree[Token] | Token], depth: int, pos: Position
    ) -> tuple[Node, ...]:
        if not rest:
            return ()
        (exprlist,) = rest
        assert isinstance(exprlist, Tree)
        return tuple(self.build(child, depth, pos) for child in exprlist.children)

    def _list_lit(self, tree: Tree[Token], depth: int, pos: Position) -> Node:
        return ListLit(pos, self._arguments(list(tree.children), depth, pos))

    def _map_lit(self, tree: Tree[Token], depth: int, pos: Position) -> Node:
        if not tree.children:
            return MapLit(pos, ())
        (inits,) = tree.children
        assert isinstance(inits, Tree)
        items = [self.build(child, depth, pos) for child in inits.children]
        return MapLit(pos, tuple(zip(items[0::2], items[1::2], strict=True)))

    def _literal(self, tree: Tree[Token], depth: int, pos: Position) -> Node:
        (token,) = tree.children
        assert isinstance(token, Token)
        kind = token.type
        text = str(token.value)
        if kind == "INT_LIT":
            negative = text.startswith("-")
            digits = text.lstrip("-")
            value = int(digits, 16) if digits[:2].lower() == "0x" else int(digits, 10)
            value = -value if negative else value
            if not INT64_MIN <= value <= INT64_MAX:
                raise syntax_error(f"integer literal {text} is outside the int64 range", pos)
            return Literal(pos, value, INT)
        if kind == "UINT_LIT":
            raise syntax_error("unsigned integers are not supported; use an int literal", pos)
        if kind == "FLOAT_LIT":
            return Literal(pos, float(text), DOUBLE)
        if kind in ("STRING_LIT", "MLSTRING_LIT"):
            try:
                string = str(celstr(token))
            except ValueError as error:
                raise syntax_error(f"invalid string literal: {error}", pos) from None
            return Literal(pos, string, string_values(frozenset({string}), enum=False))
        if kind == "BYTES_LIT":
            try:
                data = bytes(celbytes(token))
            except ValueError as error:
                raise syntax_error(f"invalid bytes literal: {error}", pos) from None
            return Literal(pos, data, BYTES)
        if kind == "BOOL_LIT":
            return Literal(pos, text == "true", BOOL)
        if kind == "NULL_LIT":
            return Literal(pos, None, NULL)
        raise syntax_error(f"unsupported literal {text!r}", pos)


def _tree_position(tree: Tree[Token], fallback: Position) -> Position:
    line = getattr(tree.meta, "line", None)
    column = getattr(tree.meta, "column", None)
    if line is None or column is None:
        return fallback
    return Position(line, column)


def _token_position(token: Token, fallback: Position) -> Position:
    if token.line is None or token.column is None:
        return fallback
    return Position(token.line, token.column)
