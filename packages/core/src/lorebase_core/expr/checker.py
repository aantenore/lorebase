"""Static type checking of an AST against an Environment.

Every node gets a type; the first problem raises ``ExprError`` (code ``expression.type``)
positioned at the offending node. ``dyn`` (json Attributes, map values of unknown type)
defers checks to evaluation.
"""

from __future__ import annotations

import difflib

from lorebase_core.expr import functions
from lorebase_core.expr.ast import (
    STEP_SYNTAX,
    STEP_VARIABLE,
    Binary,
    Call,
    Comprehension,
    Conditional,
    Ident,
    Index,
    ListLit,
    Literal,
    MapLit,
    Node,
    Select,
    Unary,
)
from lorebase_core.expr.environment import Environment
from lorebase_core.expr.errors import Position, type_error
from lorebase_core.expr.types import (
    BOOL,
    DATE,
    DECIMAL,
    DURATION,
    DYN,
    INT,
    NUMERIC,
    ORDERED,
    STRING,
    TIMESTAMP,
    ExprType,
    TypeKind,
    join,
    list_of,
    map_of,
)

K = TypeKind
_MAP_KEYS = frozenset({K.STRING, K.INT, K.BOOL, K.DYN})


class Checker:
    def __init__(self, environment: Environment) -> None:
        self.env = environment
        self.types: dict[Node, ExprType] = {}
        self.scopes: list[dict[str, ExprType]] = []

    def describe(self, expr_type: ExprType) -> str:
        return self.env.describe(expr_type)

    def check(self, node: Node) -> ExprType:
        result = self._check(node)
        self.types[node] = result
        return result

    def _check(self, node: Node) -> ExprType:
        if isinstance(node, Literal):
            return node.type
        if isinstance(node, Ident):
            return self._ident(node)
        if isinstance(node, Select):
            return self._select(node)
        if isinstance(node, Index):
            return self._index(node)
        if isinstance(node, Call):
            return self._call(node)
        if isinstance(node, Comprehension):
            return self._comprehension(node)
        if isinstance(node, ListLit):
            items = [self.check(item) for item in node.items]
            element = DYN
            if items:
                element = items[0]
                for item in items[1:]:
                    element = join(element, item)
            return list_of(element)
        if isinstance(node, MapLit):
            return self._map(node)
        if isinstance(node, Unary):
            return self._unary(node)
        if isinstance(node, Binary):
            return self._binary(node)
        if isinstance(node, Conditional):
            return self._conditional(node)
        raise type_error(f"unsupported expression {type(node).__name__}", node.pos)

    # Names and fields -------------------------------------------------------------------------

    def _ident(self, node: Ident) -> ExprType:
        for scope in reversed(self.scopes):
            if node.name in scope:
                return scope[node.name]
        found = self.env.variables.get(node.name)
        if found is not None:
            return found
        if node.name == STEP_VARIABLE:
            if self.env.step_types is None:
                raise type_error(f"{STEP_SYNTAX}[n] is only available in Operation steps", node.pos)
            raise type_error(f"use {STEP_SYNTAX}[n] with a literal step number", node.pos)
        names = [*self.env.variables, *(n for scope in self.scopes for n in scope)]
        raise type_error(f"unknown name {node.name!r}{_suggest(node.name, names)}", node.pos)

    def _select(self, node: Select) -> ExprType:
        operand = self.check(node.operand)
        result = self._field(operand, node.field, node.pos)
        return BOOL if node.test else result

    def _field(self, operand: ExprType, name: str, pos: Position) -> ExprType:
        if operand.kind is K.DYN:
            return DYN
        if operand.kind is K.MAP:
            if operand.args[0].kind not in (K.STRING, K.DYN):
                raise type_error(f"cannot select {name!r} from {self.describe(operand)}", pos)
            return operand.args[1]
        if operand.kind is K.REF:
            if name == "key":
                return STRING
            raise type_error(
                f"{self.describe(operand)} is a reference; read its attributes with "
                f"record(...).{name}",
                pos,
            )
        fields = self.env.fields_of(operand)
        if fields is None:
            raise type_error(f"{self.describe(operand)} has no fields (selecting {name!r})", pos)
        found = fields.get(name)
        if found is None:
            owner = self.describe(operand)
            if operand.kind is K.RECORD and operand.name is not None:
                owner = f"Kind {self.env.kind_name(operand.name)}"
            raise type_error(f"{owner} has no field {name!r}{_suggest(name, list(fields))}", pos)
        return found

    def _index(self, node: Index) -> ExprType:
        if (
            isinstance(node.operand, Ident)
            and node.operand.name == STEP_VARIABLE
            and STEP_VARIABLE not in self.env.variables
            and not any(STEP_VARIABLE in scope for scope in self.scopes)
        ):
            return self._step(node)
        operand = self.check(node.operand)
        index = self.check(node.index)
        if operand.kind is K.DYN:
            return DYN
        if operand.kind is K.LIST:
            if index.kind not in (K.INT, K.DYN):
                raise type_error(f"list index must be an int, got {self.describe(index)}", node.pos)
            return operand.element
        if operand.kind is K.MAP:
            if not _comparable(index, operand.args[0]):
                raise type_error(
                    f"map key must be {self.describe(operand.args[0])}, got {self.describe(index)}",
                    node.pos,
                )
            return operand.args[1]
        raise type_error(f"cannot index {self.describe(operand)}", node.pos)

    def _step(self, node: Index) -> ExprType:
        steps = self.env.step_types
        if steps is None:
            raise type_error(f"{STEP_SYNTAX}[n] is only available in Operation steps", node.pos)
        if not isinstance(node.index, Literal) or not isinstance(node.index.value, int):
            raise type_error(f"{STEP_SYNTAX}[n] needs a literal step number", node.pos)
        number = node.index.value
        if isinstance(number, bool) or not 0 <= number < len(steps):
            raise type_error(
                f"{STEP_SYNTAX}[{number}] does not name an earlier step "
                f"({len(steps)} step(s) before this one)",
                node.pos,
            )
        found = steps[number]
        if found is None:
            raise type_error(f"step {number} edits a Link, not a Record", node.pos)
        self.types[node.operand] = list_of(DYN)
        self.types[node.index] = INT
        return found

    # Calls ----------------------------------------------------------------------------------

    def _call(self, node: Call) -> ExprType:
        member = node.target is not None
        function = functions.lookup(node.function, member)
        if function is None:
            other = functions.lookup(node.function, not member)
            hint = ""
            if other is not None:
                hint = (
                    f"; call it as {node.function}(x)"
                    if member
                    else f"; call it as x.{node.function}()"
                )
            raise type_error(f"unknown function {node.function!r}{hint}", node.pos)
        if function.records and not self.env.records:
            raise type_error(
                f"{node.function}() is not available here: Derivations read only their own "
                "Record and links.<relation>",
                node.pos,
            )
        args = tuple(self.check(arg) for arg in node.args)
        if node.target is not None:
            args = (self.check(node.target), *args)
        return function.check(functions.CallSite(node, args, self.env))

    def _comprehension(self, node: Comprehension) -> ExprType:
        target = self.check(node.target)
        if target.kind is K.LIST:
            variable = target.element
        elif target.kind is K.MAP:
            variable = target.args[0]
        elif target.kind is K.DYN:
            variable = DYN
        else:
            raise type_error(
                f"{node.macro}() needs a list or a map, got {self.describe(target)}", node.pos
            )
        self.scopes.append({node.variable: variable})
        try:
            body = self.check(node.body)
        finally:
            self.scopes.pop()
        if node.macro == "map":
            return list_of(body)
        if body.kind not in (K.BOOL, K.DYN):
            raise type_error(
                f"{node.macro}() needs a bool condition, got {self.describe(body)}", node.body.pos
            )
        if node.macro == "filter":
            return target if target.kind is K.LIST else list_of(variable)
        if node.macro == "count":
            return INT
        return BOOL

    def _map(self, node: MapLit) -> ExprType:
        key_type: ExprType | None = None
        value_type: ExprType | None = None
        for key_node, value_node in node.entries:
            key = self.check(key_node)
            value = self.check(value_node)
            if key.kind not in _MAP_KEYS:
                raise type_error(
                    f"map keys must be strings, ints or bools, got {self.describe(key)}",
                    key_node.pos,
                )
            key_type = key if key_type is None else join(key_type, key)
            value_type = value if value_type is None else join(value_type, value)
        return map_of((key_type or DYN).plain(), value_type or DYN)

    # Operators ------------------------------------------------------------------------------

    def _unary(self, node: Unary) -> ExprType:
        operand = self.check(node.operand)
        if operand.kind is K.DYN:
            return BOOL if node.op == "!" else DYN
        if node.op == "!":
            if operand.kind is not K.BOOL:
                raise type_error(f"! needs a bool, got {self.describe(operand)}", node.pos)
            return BOOL
        if operand.kind not in NUMERIC | {K.DURATION}:
            raise type_error(f"cannot negate {self.describe(operand)}", node.pos)
        return operand

    def _binary(self, node: Binary) -> ExprType:
        left = self.check(node.left)
        right = self.check(node.right)
        op = node.op
        if op in ("&&", "||"):
            for side, side_type in ((node.left, left), (node.right, right)):
                if side_type.kind not in (K.BOOL, K.DYN):
                    raise type_error(
                        f"{op} needs bool operands, got {self.describe(side_type)}", side.pos
                    )
            return BOOL
        if op in ("==", "!="):
            self._require_comparable(left, right, node)
            return BOOL
        if op in ("<", "<=", ">", ">="):
            if not _orderable(left, right):
                raise type_error(
                    f"cannot order {self.describe(left)} and {self.describe(right)}", node.pos
                )
            return BOOL
        if op == "in":
            return self._in(left, right, node)
        result = _arithmetic(op, left, right)
        if result is None:
            hint = ""
            kinds = {left.kind, right.kind}
            if kinds == {K.INT, K.DOUBLE}:
                hint = "; convert with double() or int()"
            elif kinds == {K.DECIMAL, K.DOUBLE}:
                hint = "; convert with decimal()"
            raise type_error(
                f"operator {op} is not defined for {self.describe(left)} and "
                f"{self.describe(right)}{hint}",
                node.pos,
            )
        return result

    def _require_comparable(self, left: ExprType, right: ExprType, node: Node) -> None:
        if not _comparable(left, right):
            hint = ""
            if {left.kind, right.kind} == {K.REF, K.STRING}:
                hint = "; compare the reference's .key with a string"
            raise type_error(
                f"cannot compare {self.describe(left)} with {self.describe(right)}{hint}", node.pos
            )
        if (
            left.kind is K.STRING
            and right.kind is K.STRING
            and (left.enum or right.enum)
            and left.values is not None
            and right.values is not None
            and not left.values & right.values
        ):
            enum, other = (left, right) if left.enum else (right, left)
            assert other.values is not None
            assert enum.values is not None
            raise type_error(
                f"{', '.join(repr(v) for v in sorted(other.values))} is not a value of "
                f"{self.describe(enum)}: the comparison is always the same",
                node.pos,
            )

    def _in(self, left: ExprType, right: ExprType, node: Binary) -> ExprType:
        if right.kind is K.DYN:
            return BOOL
        if right.kind not in (K.LIST, K.MAP):
            raise type_error(f"'in' needs a list or a map, got {self.describe(right)}", node.pos)
        self._require_comparable(left, right.element, node)
        return BOOL

    def _conditional(self, node: Conditional) -> ExprType:
        condition = self.check(node.condition)
        if condition.kind not in (K.BOOL, K.DYN):
            raise type_error(
                f"condition must be a bool, got {self.describe(condition)}", node.condition.pos
            )
        then = self.check(node.then)
        otherwise = self.check(node.otherwise)
        result = join(then, otherwise)
        if result.kind is K.DYN and K.DYN not in (then.kind, otherwise.kind):
            raise type_error(
                f"branches have different types: {self.describe(then)} and "
                f"{self.describe(otherwise)}",
                node.pos,
            )
        return result


def _suggest(name: str, candidates: list[str]) -> str:
    close = difflib.get_close_matches(name, candidates, n=1)
    return f" (did you mean {close[0]!r}?)" if close else ""


def _comparable(left: ExprType, right: ExprType) -> bool:
    kinds = {left.kind, right.kind}
    if K.DYN in kinds or K.NULL in kinds:
        return True
    if left.is_numeric and right.is_numeric:
        return True
    if left.kind is not right.kind:
        return False
    if left.kind in (K.REF, K.RECORD, K.OBJECT):
        return left.name == right.name
    if left.kind in (K.LIST, K.MAP):
        return all(_comparable(a, b) for a, b in zip(left.args, right.args, strict=True))
    return True


def _orderable(left: ExprType, right: ExprType) -> bool:
    if K.DYN in (left.kind, right.kind):
        return True
    if left.is_numeric and right.is_numeric:
        return True
    return left.kind is right.kind and left.kind in ORDERED


_TEMPORAL: dict[tuple[str, TypeKind, TypeKind], ExprType] = {
    ("+", K.TIMESTAMP, K.DURATION): TIMESTAMP,
    ("+", K.DURATION, K.TIMESTAMP): TIMESTAMP,
    ("+", K.DATE, K.DURATION): DATE,
    ("+", K.DURATION, K.DATE): DATE,
    ("+", K.DURATION, K.DURATION): DURATION,
    ("-", K.TIMESTAMP, K.TIMESTAMP): DURATION,
    ("-", K.TIMESTAMP, K.DURATION): TIMESTAMP,
    ("-", K.DATE, K.DATE): DURATION,
    ("-", K.DATE, K.DURATION): DATE,
    ("-", K.DURATION, K.DURATION): DURATION,
}


def _arithmetic(op: str, left: ExprType, right: ExprType) -> ExprType | None:
    """Result type of ``left op right``, ``None`` when the operator does not apply."""
    if K.DYN in (left.kind, right.kind):
        other = right if left.kind is K.DYN else left
        if other.kind in (K.BOOL, K.NULL):
            return None
        return DYN
    if left.is_numeric and right.is_numeric:
        if op == "%":
            return (
                INT
                if left.kind is right.kind is K.INT
                else (DECIMAL if K.DOUBLE not in (left.kind, right.kind) else None)
            )
        if left.kind is right.kind:
            return left.plain()
        if K.DOUBLE in (left.kind, right.kind):
            return None
        return DECIMAL
    if op == "+" and left.kind is right.kind:
        if left.kind in (K.STRING, K.BYTES):
            return left.plain()
        if left.kind is K.LIST:
            return list_of(join(left.element, right.element))
    return _TEMPORAL.get((op, left.kind, right.kind))
