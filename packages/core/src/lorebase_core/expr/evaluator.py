"""Bounded, deterministic evaluation of a checked AST.

The budget comes from ``ExprLimits``: steps (one per node visited), wall-clock time (checked
every few steps, a safety net only), sizes of built strings and lists, and Record lookups.
The clock is the ``now`` the caller passes: ``now()`` and ``today()`` never read the system
time, so the same inputs give the same result. Nothing here performs I/O; Records come from
the caller's ``RecordResolver``.

``&&`` and ``||`` follow CEL: an error on one side is absorbed when the other side decides
the result. Comprehensions stop at the first deciding element and propagate errors.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context

from pydantic import JsonValue

from lorebase_core.expr import functions, ops
from lorebase_core.expr.ast import (
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
from lorebase_core.expr.errors import (
    ExprError,
    ExprErrorCode,
    evaluation_error,
    limit_error,
)
from lorebase_core.expr.limits import ExprLimits
from lorebase_core.expr.resolver import RecordResolver
from lorebase_core.expr.values import (
    LinksValue,
    ObjectValue,
    RecordValue,
    Ref,
    type_name,
)
from lorebase_core.model.record import format_ref
from lorebase_core.types import GeoPoint

#: Steps between two wall-clock checks.
_CLOCK_EVERY = 64

Overlay = Mapping[tuple[str, str], Mapping[str, JsonValue] | None]


class Evaluator:
    """Evaluates one expression; implements ``functions.Runtime``."""

    def __init__(
        self,
        environment: Environment,
        variables: Mapping[str, object],
        *,
        now: datetime,
        limits: ExprLimits,
        resolver: RecordResolver | None = None,
        overlay: Overlay | None = None,
    ) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("the evaluation clock must be timezone-aware")
        self.env = environment
        self.variables = variables
        self._now = now
        self._limits = limits
        self.resolver = resolver
        self.overlay = overlay or {}
        self._decimals = Context(prec=limits.decimal_precision, rounding=ROUND_HALF_EVEN)
        self.scopes: list[dict[str, object]] = []
        self.steps = 0
        self.lookups = 0
        self.deadline = time.monotonic() + limits.max_duration_ms / 1000
        self._patterns: dict[str, Callable[[str], bool]] = {}

    # Runtime protocol ---------------------------------------------------------------------------

    @property
    def now(self) -> datetime:
        return self._now

    @property
    def limits(self) -> ExprLimits:
        return self._limits

    @property
    def decimals(self) -> Context:
        return self._decimals

    def text(self, value: str) -> str:
        if len(value) > self._limits.max_string_length:
            raise limit_error(f"string longer than {self._limits.max_string_length} characters")
        return value

    def items(self, value: list[object]) -> list[object]:
        if len(value) > self._limits.max_list_size:
            raise limit_error(f"list longer than {self._limits.max_list_size} elements")
        return value

    def regex(self, pattern: str) -> Callable[[str], bool]:
        compiled = self._patterns.get(pattern)
        if compiled is None:
            try:
                compiled = functions.compile_regex(pattern)
            except ValueError as error:
                raise evaluation_error(str(error)) from None
            self._patterns[pattern] = compiled
        return compiled

    def _count_lookup(self) -> RecordResolver:
        self.lookups += 1
        if self.lookups > self._limits.max_record_lookups:
            raise limit_error(f"more than {self._limits.max_record_lookups} Record lookups")
        if self.resolver is None:
            raise evaluation_error("Records cannot be read in this evaluation")
        return self.resolver

    def load_record(self, ref: Ref) -> RecordValue:
        kind = self.env.schema.kinds.get(ref.kind)
        if kind is None:
            raise evaluation_error(f"unknown Kind {ref.kind}")
        if (ref.kind, ref.key) in self.overlay:
            self._count_lookup()
            values = self.overlay[(ref.kind, ref.key)]
        else:
            values = self._count_lookup().record(ref.kind, ref.key)
        if values is None:
            raise ExprError(
                ExprErrorCode.RECORD_NOT_FOUND,
                f"Record {format_ref(kind.api_name, ref.key)} does not exist",
            )
        return RecordValue(kind, ref.key, values)

    def load_links(self, ref: Ref, name: str) -> list[RecordValue]:
        found = self.env.schema.link_by_api_name(name)
        if found is None:
            raise evaluation_error(f"unknown Relation {name!r}")
        relation, direction = found
        if relation.endpoint(direction) != ref.kind:
            raise evaluation_error(f"Relation {name!r} does not start at this Record's Kind")
        far = self.env.schema.kinds.get(relation.far_endpoint(direction))
        if far is None:
            raise evaluation_error(f"Relation {name!r} ends at an unknown Kind")
        linked = self._count_lookup().linked(relation.id, direction, ref.key)
        self.items(list(linked))
        return [RecordValue(far, item.key, item.values) for item in linked]

    # Evaluation -----------------------------------------------------------------------------------

    def evaluate(self, node: Node) -> object:
        try:
            return self.eval(node)
        except RecursionError:
            raise limit_error("expression nests too deeply to evaluate") from None

    def eval(self, node: Node) -> object:
        self.steps += 1
        if self.steps > self._limits.max_steps:
            raise limit_error(f"evaluation took more than {self._limits.max_steps} steps", node.pos)
        if self.steps % _CLOCK_EVERY == 0 and time.monotonic() > self.deadline:
            raise limit_error(
                f"evaluation took longer than {self._limits.max_duration_ms} ms", node.pos
            )
        try:
            return self._eval(node)
        except ExprError as error:
            if error.position is None:
                error.position = node.pos
            raise

    def _eval(self, node: Node) -> object:
        if isinstance(node, Literal):
            return node.value
        if isinstance(node, Ident):
            return self._ident(node)
        if isinstance(node, Select):
            operand = self.eval(node.operand)
            if node.test:
                return self._has(operand, node.field)
            return self._field(operand, node.field)
        if isinstance(node, Index):
            return self._index(self.eval(node.operand), self.eval(node.index))
        if isinstance(node, Call):
            return self._call(node)
        if isinstance(node, Comprehension):
            return self._comprehension(node)
        if isinstance(node, ListLit):
            return self.items([self.eval(item) for item in node.items])
        if isinstance(node, MapLit):
            return self._map(node)
        if isinstance(node, Unary):
            operand = self.eval(node.operand)
            if node.op == "!":
                return not ops.require_bool(operand, "! operand")
            return ops.negate(operand, self._decimals)
        if isinstance(node, Binary):
            return self._binary(node)
        if isinstance(node, Conditional):
            condition = ops.require_bool(self.eval(node.condition), "condition")
            return self.eval(node.then if condition else node.otherwise)
        raise evaluation_error(f"unsupported expression {type(node).__name__}")

    def _ident(self, node: Ident) -> object:
        for scope in reversed(self.scopes):
            if node.name in scope:
                return scope[node.name]
        if node.name in self.variables:
            return self.variables[node.name]
        raise evaluation_error(f"no value for {node.name!r}")

    def _field(self, operand: object, name: str) -> object:
        if isinstance(operand, RecordValue):
            if name == "key":
                return operand.key
            if name == "ref":
                return operand.ref
            return operand.get(name)
        if isinstance(operand, LinksValue):
            return self._links_field(operand, name)
        if isinstance(operand, ObjectValue):
            if name not in operand.fields:
                raise evaluation_error(f"{operand.name} has no field {name!r}")
            return operand.fields[name]
        if isinstance(operand, GeoPoint) and name in ("lat", "lon"):
            return getattr(operand, name)
        if isinstance(operand, dict):
            if name not in operand:
                raise evaluation_error(f"no key {name!r} in map")
            return operand[name]
        if isinstance(operand, Ref) and name == "key":
            return operand.key
        if operand is None:
            raise evaluation_error(
                f"cannot read {name!r} of an absent value (null); test presence with has()"
            )
        raise evaluation_error(f"{type_name(operand)} has no field {name!r}")

    def _has(self, operand: object, name: str) -> bool:
        if isinstance(operand, RecordValue):
            return name in ("key", "ref") or operand.has(name)
        if isinstance(operand, ObjectValue):
            return operand.fields.get(name) is not None
        if isinstance(operand, dict):
            return name in operand
        if isinstance(operand, LinksValue | GeoPoint):
            return True
        if operand is None:
            raise evaluation_error(f"has() on an absent value (null) selecting {name!r}")
        raise evaluation_error(f"has() is not defined on {type_name(operand)}")

    def _links_field(self, links: LinksValue, name: str) -> list[RecordValue]:
        field = self.env.link_fields.get(name)
        if field is None:
            raise evaluation_error(f"no Relation {name!r} at Kind {links.kind.api_name}")
        far = self.env.schema.kinds.get(field.relation.far_endpoint(field.direction))
        if far is None:
            raise evaluation_error(f"Relation {name!r} ends at an unknown Kind")
        linked = self._count_lookup().linked(field.relation.id, field.direction, links.key)
        self.items(list(linked))
        return [RecordValue(far, item.key, item.values) for item in linked]

    def _index(self, operand: object, index: object) -> object:
        if isinstance(operand, list):
            if isinstance(index, bool) or not isinstance(index, int):
                raise evaluation_error(f"list index must be an int, got {type_name(index)}")
            if not 0 <= index < len(operand):
                raise evaluation_error(f"index {index} out of range for a list of {len(operand)}")
            return operand[index]
        if isinstance(operand, dict):
            for key, value in operand.items():
                if ops.equals(key, index):
                    return value
            raise evaluation_error(f"no key {index!r} in map")
        if operand is None:
            raise evaluation_error("cannot index an absent value (null)")
        raise evaluation_error(f"cannot index {type_name(operand)}")

    def _call(self, node: Call) -> object:
        function = functions.lookup(node.function, node.target is not None)
        if function is None:
            raise evaluation_error(f"unknown function {node.function!r}")
        args = [self.eval(arg) for arg in node.args]
        if node.target is not None:
            args.insert(0, self.eval(node.target))
        try:
            result = function.run(self, args)
        except (ArithmeticError, ValueError) as error:
            raise evaluation_error(f"{node.function}() failed: {error}") from None
        if isinstance(result, str):
            self.text(result)
        return result

    def _comprehension(self, node: Comprehension) -> object:
        target = self.eval(node.target)
        if isinstance(target, list):
            elements: list[object] = target
        elif isinstance(target, dict):
            elements = list(target)
        else:
            raise evaluation_error(f"{node.macro}() needs a list or a map, got {type_name(target)}")
        scope: dict[str, object] = {}
        self.scopes.append(scope)
        try:
            return self._iterate(node, elements, scope)
        finally:
            self.scopes.pop()

    def _iterate(
        self, node: Comprehension, elements: list[object], scope: dict[str, object]
    ) -> object:
        macro = node.macro
        results: list[object] = []
        matches = 0
        for element in elements:
            scope[node.variable] = element
            value = self.eval(node.body)
            if macro == "map":
                results.append(value)
                self.items(results)
                continue
            accepted = ops.require_bool(value, f"{macro}() condition")
            if macro == "all" and not accepted:
                return False
            if macro == "exists" and accepted:
                return True
            if accepted:
                matches += 1
                if macro == "filter":
                    results.append(element)
        if macro == "all":
            return True
        if macro == "exists":
            return False
        if macro == "exists_one":
            return matches == 1
        if macro == "count":
            return matches
        return results

    def _map(self, node: MapLit) -> dict[object, object]:
        result: dict[object, object] = {}
        for key_node, value_node in node.entries:
            key = self.eval(key_node)
            if not isinstance(key, str | int):
                raise evaluation_error(
                    f"map keys must be strings, ints or bools, got {type_name(key)}"
                )
            if any(ops.equals(key, existing) for existing in result):
                raise evaluation_error(f"duplicate map key {key!r}")
            result[key] = self.eval(value_node)
        self.items(list(result))
        return result

    def _binary(self, node: Binary) -> object:
        op = node.op
        if op in ("&&", "||"):
            return self._logical(node)
        left = self.eval(node.left)
        right = self.eval(node.right)
        if op == "==":
            return ops.equals(left, right)
        if op == "!=":
            return not ops.equals(left, right)
        if op == "<":
            return ops.compare(left, right) < 0
        if op == "<=":
            return ops.compare(left, right) <= 0
        if op == ">":
            return ops.compare(left, right) > 0
        if op == ">=":
            return ops.compare(left, right) >= 0
        if op == "in":
            return ops.contains(right, left)
        result = ops.arithmetic(op, left, right, self._decimals)
        if isinstance(result, str):
            self.text(result)
        elif isinstance(result, list):
            self.items(result)
        elif isinstance(result, bytes) and len(result) > self._limits.max_string_length:
            raise limit_error(f"bytes longer than {self._limits.max_string_length}")
        return result

    def _logical(self, node: Binary) -> bool:
        deciding = node.op == "||"  # the value that decides the result on its own
        failure: ExprError | None = None
        try:
            left = ops.require_bool(self.eval(node.left), f"{node.op} operand")
        except ExprError as error:
            if error.code is ExprErrorCode.LIMIT_EXCEEDED:
                raise
            failure = error
        else:
            if left is deciding:
                return deciding
        right = ops.require_bool(self.eval(node.right), f"{node.op} operand")
        if right is deciding:
            return deciding
        if failure is not None:
            raise failure
        return not deciding
