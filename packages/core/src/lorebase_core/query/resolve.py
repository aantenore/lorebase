"""Validation of DSL queries against a Schema, producing a resolved plan keyed by ids.

`resolve_query` maps api names to Kind, Attribute and Relation ids, checks every operator
against the Attribute type, encodes operands into canonical values (the form stored in
`values`) and enforces the limits given by the caller. All problems of a query are collected
and raised together as `QueryError`, each with a JSON pointer into the query document.

Operators per Attribute type:

- string, text: eq, ne, lt, lte, gt, gte, in, between, prefix, match, exists
- uri, ref: eq, ne, lt, lte, gt, gte, in, between, prefix, exists
- enum: eq, ne, in, exists
- integer, decimal, float, date, timestamp, duration: eq, ne, lt, lte, gt, gte, in, between,
  exists
- boolean: eq, ne, exists
- array: contains, exists
- geo_point: near, exists
- json: exists

`ne` matches Records that hold a value different from the operand (absent values never
match a comparison); `exists` takes `true` (default) or `false`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from lorebase_core.model import (
    Attribute,
    Kind,
    LinkDirection,
    PathRelation,
    Relation,
    Schema,
    Trait,
)
from lorebase_core.model.relation import RelationBacking
from lorebase_core.query.dsl import (
    AllOf,
    AnyOf,
    AttributeCondition,
    CountCondition,
    DerivedCondition,
    MatchCondition,
    NotCondition,
    Operator,
    Predicate,
    Query,
    ViaCondition,
)
from lorebase_core.types import AttributeType, EncodingError, TypeSpec, encode_value

__all__ = [
    "KEY_FIELD",
    "OPERATORS",
    "TITLE_FIELD",
    "AggregatePlan",
    "Comparison",
    "Condition",
    "Conjunction",
    "Disjunction",
    "FieldRef",
    "FieldSource",
    "FullText",
    "Hop",
    "IncludePlan",
    "LinkCount",
    "LinkExists",
    "Negation",
    "Ordering",
    "QueryError",
    "QueryIssue",
    "QueryLimits",
    "QueryPlan",
    "Target",
    "resolve_field",
    "resolve_hop",
    "resolve_query",
    "resolve_where",
    "target_of_kind",
    "target_of_trait",
]

KEY_FIELD = "key"
"""Pseudo field: the Record key (a reserved Attribute name)."""
TITLE_FIELD = "title"
"""Pseudo field: the Record title, unless the Kind has an Attribute named `title`."""

_TEXT = TypeSpec.of(AttributeType.STRING)
_ORDERED = frozenset(
    {Operator.EQ, Operator.NE, Operator.LT, Operator.LTE, Operator.GT, Operator.GTE}
    | {Operator.IN, Operator.BETWEEN, Operator.EXISTS}
)

OPERATORS: Mapping[AttributeType, frozenset[Operator]] = MappingProxyType(
    {
        AttributeType.STRING: _ORDERED | {Operator.PREFIX, Operator.MATCH},
        AttributeType.TEXT: _ORDERED | {Operator.PREFIX, Operator.MATCH},
        AttributeType.URI: _ORDERED | {Operator.PREFIX},
        AttributeType.REF: _ORDERED | {Operator.PREFIX},
        AttributeType.ENUM: frozenset({Operator.EQ, Operator.NE, Operator.IN, Operator.EXISTS}),
        AttributeType.INTEGER: _ORDERED,
        AttributeType.DECIMAL: _ORDERED,
        AttributeType.FLOAT: _ORDERED,
        AttributeType.DATE: _ORDERED,
        AttributeType.TIMESTAMP: _ORDERED,
        AttributeType.DURATION: _ORDERED,
        AttributeType.BOOLEAN: frozenset({Operator.EQ, Operator.NE, Operator.EXISTS}),
        AttributeType.ARRAY: frozenset({Operator.CONTAINS, Operator.EXISTS}),
        AttributeType.GEO_POINT: frozenset({Operator.NEAR, Operator.EXISTS}),
        AttributeType.JSON: frozenset({Operator.EXISTS}),
    }
)
"""Operators allowed per Attribute type."""

ORDERABLE_TYPES = frozenset(
    t for t, ops in OPERATORS.items() if Operator.LT in ops or t is AttributeType.BOOLEAN
) | {AttributeType.ENUM}
"""Types usable in `order_by`."""

COUNT_OPERATORS = frozenset(
    {Operator.EQ, Operator.NE, Operator.LT, Operator.LTE, Operator.GT, Operator.GTE}
    | {Operator.BETWEEN}
)
"""Operators of `count` predicates."""


# Issues -------------------------------------------------------------------------------------


class IssueCode(StrEnum):
    UNKNOWN_KIND = "unknown_kind"
    UNKNOWN_TRAIT = "unknown_trait"
    UNKNOWN_FIELD = "unknown_field"
    UNKNOWN_RELATION = "unknown_relation"
    RELATION_NOT_ON_KIND = "relation_not_on_kind"
    OPERATOR_NOT_ALLOWED = "operator_not_allowed"
    INVALID_VALUE = "invalid_value"
    NOT_DERIVED = "not_derived"
    NOT_ORDERABLE = "not_orderable"
    LIMIT_EXCEEDED = "limit_exceeded"
    DEPTH_EXCEEDED = "depth_exceeded"
    TOO_COMPLEX = "too_complex"


@dataclass(frozen=True, slots=True)
class QueryIssue:
    """One problem: JSON pointer into the query document, stable code, message."""

    pointer: str
    code: str
    message: str


class QueryError(ValueError):
    """A query does not fit the Schema or the limits; `issues` lists every problem."""

    def __init__(self, issues: Sequence[QueryIssue]) -> None:
        super().__init__("; ".join(f"{i.pointer}: {i.message}" for i in issues))
        self.issues = tuple(issues)


@dataclass(frozen=True, slots=True)
class QueryLimits:
    """Bounds enforced while resolving a query (from the caller's configuration)."""

    default_limit: int = 50
    max_limit: int = 500
    max_via_depth: int = 3
    max_predicates: int = 64
    max_in_values: int = 1000
    include_default_limit: int = 10
    include_max_limit: int = 50
    max_includes: int = 5
    max_order_terms: int = 4


# Resolved plan ------------------------------------------------------------------------------


class FieldSource(StrEnum):
    VALUE = "value"
    DERIVED = "derived"
    KEY = "key"
    TITLE = "title"


@dataclass(frozen=True, slots=True)
class FieldRef:
    """A field of the target Records. `attributes` maps Kind id to Attribute id."""

    name: str
    source: FieldSource
    spec: TypeSpec
    attributes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Target:
    """The Kinds a query reads; a Trait target expands to its implementing Kinds."""

    name: str
    kinds: tuple[Kind, ...]
    trait: Trait | None = None

    @property
    def kind_ids(self) -> tuple[str, ...]:
        return tuple(kind.id for kind in self.kinds)


@dataclass(frozen=True, slots=True)
class Hop:
    """One step along a Relation from `near_kind` to `far`.

    Along a path Derivation, `relation` is its virtual Relation and `path` the resolved path
    (`lorebase_core.model.paths`): readers compose its steps, or read its stored Links when it
    is materialized.
    """

    name: str
    relation: Relation
    direction: LinkDirection
    near_kind: str
    far: Target
    path: PathRelation | None = None
    inner: tuple[Kind, ...] = ()

    @property
    def foreign_key(self) -> bool:
        return self.relation.backing is RelationBacking.FOREIGN_KEY

    @property
    def kinds_read(self) -> frozenset[str]:
        """Api names of the Kinds the hop reads: the far Kinds and, along a path, the Kinds
        of its intermediate Records (a path tells which Records connect through them)."""
        return frozenset(kind.api_name for kind in (*self.far.kinds, *self.inner))


@dataclass(frozen=True, slots=True)
class Comparison:
    field: FieldRef
    op: Operator
    operand: Any = None


@dataclass(frozen=True, slots=True)
class LinkExists:
    hop: Hop
    where: Condition | None = None


@dataclass(frozen=True, slots=True)
class LinkCount:
    hop: Hop
    op: Operator
    operand: Any
    where: Condition | None = None


@dataclass(frozen=True, slots=True)
class FullText:
    text: str


@dataclass(frozen=True, slots=True)
class Conjunction:
    items: tuple[Condition, ...]


@dataclass(frozen=True, slots=True)
class Disjunction:
    items: tuple[Condition, ...]


@dataclass(frozen=True, slots=True)
class Negation:
    item: Condition


Condition = Comparison | LinkExists | LinkCount | FullText | Conjunction | Disjunction | Negation


@dataclass(frozen=True, slots=True)
class Ordering:
    field: FieldRef
    descending: bool = False


@dataclass(frozen=True, slots=True)
class IncludePlan:
    hop: Hop
    select: tuple[FieldRef, ...] | None
    limit: int
    where: Condition | None = None


@dataclass(frozen=True, slots=True)
class AggregatePlan:
    group_by: FieldRef | None = None


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """A validated query: everything resolved to ids, operands in canonical form."""

    target: Target
    where: Condition | None
    select: tuple[FieldRef, ...] | None
    include: tuple[IncludePlan, ...]
    order: tuple[Ordering, ...]
    limit: int
    cursor: str | None
    aggregate: AggregatePlan | None


def condition_kinds(condition: Condition | None) -> frozenset[str]:
    """Api names of the Kinds a condition reaches through Relation predicates."""
    match condition:
        case LinkExists(hop=hop, where=where) | LinkCount(hop=hop, where=where):
            return hop.kinds_read | condition_kinds(where)
        case Conjunction(items=items) | Disjunction(items=items):
            return frozenset().union(*(condition_kinds(item) for item in items))
        case Negation(item=item):
            return condition_kinds(item)
    return frozenset()


def plan_kinds(plan: QueryPlan) -> frozenset[str]:
    """Api names of every Kind a query reads: its target, Relation predicates and includes."""
    kinds = {k.api_name for k in plan.target.kinds} | condition_kinds(plan.where)
    for include in plan.include:
        kinds |= include.hop.kinds_read | condition_kinds(include.where)
    return frozenset(kinds)


# Resolution -------------------------------------------------------------------------------


def target_of_kind(schema: Schema, api_name: str) -> Target | None:
    kind = schema.kind_by_api_name(api_name)
    return None if kind is None else Target(api_name, (kind,))


def target_of_trait(schema: Schema, api_name: str) -> Target | None:
    trait = schema.trait_by_api_name(api_name)
    if trait is None:
        return None
    kinds = tuple(sorted(schema.kinds_implementing(trait.id), key=lambda k: k.id))
    return Target(api_name, kinds, trait)


def resolve_field(schema: Schema, target: Target, name: str) -> FieldRef | None:
    """Resolve an Attribute, derived Attribute, `key` or `title` of the target Records."""
    if name == KEY_FIELD:
        return FieldRef(name, FieldSource.KEY, _TEXT)
    if target.trait is not None:
        found = _trait_field(target, name)
    else:
        found = _kind_field(target.kinds[0], name)
    if found is None and name == TITLE_FIELD:
        return FieldRef(name, FieldSource.TITLE, _TEXT)
    return found


def _kind_field(kind: Kind, name: str) -> FieldRef | None:
    attribute = kind.attribute_by_api_name(name)
    if attribute is None:
        return None
    source = FieldSource.DERIVED if attribute.is_derived else FieldSource.VALUE
    return FieldRef(name, source, attribute.type, MappingProxyType({kind.id: attribute.id}))


def _trait_field(target: Target, name: str) -> FieldRef | None:
    assert target.trait is not None
    trait_attribute = next(
        (a for a in target.trait.attributes.values() if a.api_name == name), None
    )
    if trait_attribute is None:
        return None
    mapped: dict[str, str] = {}
    derived = False
    for kind in target.kinds:
        for implementation in kind.traits:
            if implementation.trait_id != target.trait.id:
                continue
            attribute_id = implementation.attribute_map.get(trait_attribute.id)
            attribute: Attribute | None = (
                kind.attributes.get(attribute_id) if attribute_id is not None else None
            )
            if attribute is not None:
                mapped[kind.id] = attribute.id
                derived = derived or attribute.is_derived
    source = FieldSource.DERIVED if derived else FieldSource.VALUE
    return FieldRef(name, source, trait_attribute.type, MappingProxyType(mapped))


def resolve_hop(schema: Schema, target: Target, name: str) -> Hop | str:
    """Resolve a forward or inverse Relation name (or path Derivation name) from the target;
    a message when invalid."""
    found = schema.relation_by_api_name(name)
    path: PathRelation | None = None
    if found is None:
        along = schema.path_by_api_name(name)
        if along is None:
            return f"unknown Relation {name!r}"
        path, direction = along
        found = (path.relation, direction)
    relation, direction = found
    near = relation.endpoint(direction)
    if near not in target.kind_ids:
        return f"Relation {name!r} does not start at {target.name}"
    far_kind = schema.kinds.get(relation.far_endpoint(direction))
    if far_kind is None:
        return f"Relation {name!r} points to an unknown Kind"
    inner: tuple[Kind, ...] = ()
    if path is not None:
        found_inner = [schema.kinds.get(kind_id) for kind_id in path.inner_kinds]
        if any(kind is None for kind in found_inner):
            return f"Relation {name!r} crosses an unknown Kind"
        inner = tuple(kind for kind in found_inner if kind is not None)
    return Hop(name, relation, direction, near, Target(far_kind.api_name, (far_kind,)), path, inner)


@dataclass
class _Resolver:
    schema: Schema
    limits: QueryLimits
    issues: list[QueryIssue] = field(default_factory=list)
    predicates: int = 0

    def fail(self, pointer: str, code: IssueCode, message: str) -> None:
        self.issues.append(QueryIssue(pointer or "/", code.value, message))

    # Predicates -------------------------------------------------------------------------

    def condition(
        self, predicate: Predicate, target: Target, pointer: str, depth: int
    ) -> Condition | None:
        self.predicates += 1
        if self.predicates == self.limits.max_predicates + 1:
            self.fail(
                pointer,
                IssueCode.TOO_COMPLEX,
                f"a query holds at most {self.limits.max_predicates} predicates",
            )
        match predicate:
            case AllOf(items=items):
                parts = [
                    self.condition(p, target, f"{pointer}/and/{i}", depth)
                    for i, p in enumerate(items)
                ]
                return _all(parts, Conjunction)
            case AnyOf(items=items):
                parts = [
                    self.condition(p, target, f"{pointer}/or/{i}", depth)
                    for i, p in enumerate(items)
                ]
                return _all(parts, Disjunction)
            case NotCondition(item=item):
                inner = self.condition(item, target, f"{pointer}/not", depth)
                return None if inner is None else Negation(inner)
            case MatchCondition(match=text):
                return FullText(text)
            case ViaCondition(via=name, where=where):
                hop = self.hop(target, name, f"{pointer}/via", depth)
                if hop is None:
                    return None
                inner = (
                    None
                    if where is None
                    else self.condition(where, hop.far, f"{pointer}/where", depth + 1)
                )
                if where is not None and inner is None:
                    return None
                return LinkExists(hop, inner)
            case CountCondition(count=name, op=op, value=value, where=where):
                hop = self.hop(target, name, f"{pointer}/count", depth)
                operand = self.count_operand(op, value, pointer)
                inner = (
                    None
                    if where is None or hop is None
                    else self.condition(where, hop.far, f"{pointer}/where", depth + 1)
                )
                if hop is None or operand is None or (where is not None and inner is None):
                    return None
                return LinkCount(hop, op, operand, inner)
            case DerivedCondition(derived=name, op=op, value=value):
                ref = resolve_field(self.schema, target, name)
                if ref is None or ref.source is not FieldSource.DERIVED:
                    code = IssueCode.UNKNOWN_FIELD if ref is None else IssueCode.NOT_DERIVED
                    self.fail(f"{pointer}/derived", code, f"no derived Attribute {name!r}")
                    return None
                return self.comparison(ref, op, value, pointer)
            case AttributeCondition(attr=name, op=op, value=value):
                ref = resolve_field(self.schema, target, name)
                if ref is None:
                    self.fail(
                        f"{pointer}/attr",
                        IssueCode.UNKNOWN_FIELD,
                        f"{target.name} has no Attribute {name!r}",
                    )
                    return None
                return self.comparison(ref, op, value, pointer)
        raise AssertionError(f"unexpected predicate {predicate!r}")  # pragma: no cover

    def hop(self, target: Target, name: str, pointer: str, depth: int) -> Hop | None:
        if depth >= self.limits.max_via_depth:
            self.fail(
                pointer,
                IssueCode.DEPTH_EXCEEDED,
                f"Relations nest at most {self.limits.max_via_depth} levels deep",
            )
            return None
        hop = resolve_hop(self.schema, target, name)
        if isinstance(hop, str):
            code = (
                IssueCode.UNKNOWN_RELATION
                if self.schema.relation_by_api_name(name) is None
                else IssueCode.RELATION_NOT_ON_KIND
            )
            self.fail(pointer, code, hop)
            return None
        return hop

    def comparison(
        self, ref: FieldRef, op: Operator, value: Any, pointer: str
    ) -> Comparison | None:
        allowed = OPERATORS[ref.spec.name]
        if op not in allowed:
            self.fail(
                f"{pointer}/op",
                IssueCode.OPERATOR_NOT_ALLOWED,
                f"{op.value} does not apply to {ref.name} ({ref.spec.describe()}); "
                f"use one of {', '.join(sorted(o.value for o in allowed))}",
            )
            return None
        operand = self.operand(ref, op, value, f"{pointer}/value")
        return None if operand is _INVALID else Comparison(ref, op, operand)

    def operand(self, ref: FieldRef, op: Operator, value: Any, pointer: str) -> Any:
        spec = ref.spec
        if op is Operator.EXISTS:
            if value is None:
                return True
            if not isinstance(value, bool):
                return self.invalid(pointer, "exists takes true or false")
            return value
        if op is Operator.IN:
            if not isinstance(value, list) or not value:
                return self.invalid(pointer, "in takes a non-empty list")
            if len(value) > self.limits.max_in_values:
                self.fail(
                    pointer,
                    IssueCode.LIMIT_EXCEEDED,
                    f"in takes at most {self.limits.max_in_values} values",
                )
                return _INVALID
            encoded = [self.encode(spec, item, f"{pointer}/{i}") for i, item in enumerate(value)]
            return _INVALID if _INVALID in encoded else tuple(dict.fromkeys(encoded))
        if op is Operator.BETWEEN:
            if not isinstance(value, list) or len(value) != 2:
                return self.invalid(pointer, "between takes a list [low, high]")
            low, high = (self.encode(spec, item, f"{pointer}/{i}") for i, item in enumerate(value))
            return _INVALID if _INVALID in (low, high) else (low, high)
        if op is Operator.CONTAINS:
            element = spec.scalar
            items = value if isinstance(value, list) else [value]
            if not items:
                return self.invalid(pointer, "contains takes a value or a non-empty list")
            encoded = [self.encode(element, item, f"{pointer}/{i}") for i, item in enumerate(items)]
            return _INVALID if _INVALID in encoded else tuple(encoded)
        if op is Operator.PREFIX or op is Operator.MATCH:
            if not isinstance(value, str) or not value:
                return self.invalid(pointer, f"{op.value} takes a non-empty string")
            return value
        if op is Operator.NEAR:
            return self.near(value, pointer)
        return self.encode(spec, value, pointer)

    def near(self, value: Any, pointer: str) -> Any:
        if not isinstance(value, dict) or set(value) != {"lat", "lon", "radius_m"}:
            return self.invalid(pointer, "near takes {lat, lon, radius_m}")
        point = self.encode(
            TypeSpec.of(AttributeType.GEO_POINT),
            {"lat": value["lat"], "lon": value["lon"]},
            pointer,
        )
        radius = value["radius_m"]
        if (
            isinstance(radius, bool)
            or not isinstance(radius, int | float)
            or not radius > 0
            or point is _INVALID
        ):
            return self.invalid(pointer, "near takes a point and a positive radius_m")
        return (point["lat"], point["lon"], float(radius))

    def count_operand(self, op: Operator, value: Any, pointer: str) -> Any:
        if op not in COUNT_OPERATORS:
            self.fail(
                f"{pointer}/op",
                IssueCode.OPERATOR_NOT_ALLOWED,
                f"count takes one of {', '.join(sorted(o.value for o in COUNT_OPERATORS))}",
            )
            return None
        if op is Operator.BETWEEN:
            if isinstance(value, list) and len(value) == 2 and all(_count(v) for v in value):
                return (value[0], value[1])
        elif _count(value):
            return value
        self.invalid(f"{pointer}/value", "count compares with non-negative integers")
        return None

    def encode(self, spec: TypeSpec, value: Any, pointer: str) -> Any:
        if value is None:
            return self.invalid(pointer, "a value is required")
        try:
            return encode_value(spec, value)
        except EncodingError as error:
            return self.invalid(pointer, f"not a valid {spec.describe()}: {error}")

    def invalid(self, pointer: str, message: str) -> Any:
        self.fail(pointer, IssueCode.INVALID_VALUE, message)
        return _INVALID

    # Query parts ------------------------------------------------------------------------

    def target(self, query: Query) -> Target | None:
        if query.kind is not None:
            found = target_of_kind(self.schema, query.kind)
            if found is None:
                self.fail("/kind", IssueCode.UNKNOWN_KIND, f"unknown Kind {query.kind!r}")
            return found
        assert query.trait is not None
        found = target_of_trait(self.schema, query.trait)
        if found is None:
            self.fail("/trait", IssueCode.UNKNOWN_TRAIT, f"unknown Trait {query.trait!r}")
        return found

    def select(
        self, target: Target, names: Sequence[str] | None, pointer: str
    ) -> tuple[FieldRef, ...] | None:
        if names is None:
            return None
        refs: list[FieldRef] = []
        for i, name in enumerate(dict.fromkeys(names)):
            ref = resolve_field(self.schema, target, name)
            if ref is None:
                self.fail(
                    f"{pointer}/{i}", IssueCode.UNKNOWN_FIELD, f"{target.name} has no {name!r}"
                )
            else:
                refs.append(ref)
        return tuple(refs)

    def order(self, target: Target, query: Query) -> tuple[Ordering, ...]:
        if len(query.order_by) > self.limits.max_order_terms:
            self.fail(
                "/order_by",
                IssueCode.LIMIT_EXCEEDED,
                f"order_by takes at most {self.limits.max_order_terms} terms",
            )
        terms: list[Ordering] = []
        for i, term in enumerate(query.order_by):
            ref = resolve_field(self.schema, target, term.attr)
            if ref is None:
                self.fail(
                    f"/order_by/{i}/attr",
                    IssueCode.UNKNOWN_FIELD,
                    f"{target.name} has no {term.attr!r}",
                )
            elif ref.spec.name not in ORDERABLE_TYPES:
                self.fail(
                    f"/order_by/{i}/attr",
                    IssueCode.NOT_ORDERABLE,
                    f"{ref.spec.describe()} values cannot be ordered",
                )
            else:
                terms.append(Ordering(ref, term.dir == "desc"))
        return tuple(terms)

    def includes(self, target: Target, query: Query) -> tuple[IncludePlan, ...]:
        if len(query.include) > self.limits.max_includes:
            self.fail(
                "/include",
                IssueCode.LIMIT_EXCEEDED,
                f"include takes at most {self.limits.max_includes} Relations",
            )
        plans: list[IncludePlan] = []
        for name, spec in query.include.items():
            pointer = f"/include/{_escape(name)}"
            hop = self.hop(target, name, pointer, 0)
            if hop is None:
                continue
            limit = spec.limit or self.limits.include_default_limit
            if limit > self.limits.include_max_limit:
                self.fail(
                    f"{pointer}/limit",
                    IssueCode.LIMIT_EXCEEDED,
                    f"must not exceed {self.limits.include_max_limit}",
                )
            where = (
                None
                if spec.where is None
                else self.condition(spec.where, hop.far, f"{pointer}/where", 1)
            )
            select = self.select(hop.far, spec.select, f"{pointer}/select")
            plans.append(IncludePlan(hop, select, limit, where))
        return tuple(plans)

    def limit(self, query: Query) -> int:
        limit = self.limits.default_limit if query.limit is None else query.limit
        if limit > self.limits.max_limit:
            self.fail(
                "/limit", IssueCode.LIMIT_EXCEEDED, f"must not exceed {self.limits.max_limit}"
            )
        return limit

    def aggregate(self, target: Target, query: Query) -> AggregatePlan | None:
        if query.aggregate is None:
            return None
        if query.aggregate.group_by is None:
            return AggregatePlan()
        ref = resolve_field(self.schema, target, query.aggregate.group_by)
        if ref is None:
            self.fail(
                "/aggregate/group_by",
                IssueCode.UNKNOWN_FIELD,
                f"{target.name} has no {query.aggregate.group_by!r}",
            )
            return None
        if ref.spec.name not in ORDERABLE_TYPES:
            self.fail(
                "/aggregate/group_by",
                IssueCode.NOT_ORDERABLE,
                f"{ref.spec.describe()} values cannot be grouped",
            )
            return None
        return AggregatePlan(ref)


_INVALID: Any = object()


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _all[T: (Conjunction, Disjunction)](parts: list[Condition | None], kind: type[T]) -> T | None:
    if any(part is None for part in parts):
        return None
    return kind(tuple(part for part in parts if part is not None))


def _escape(token: str) -> str:
    """JSON pointer escaping (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


def resolve_query(query: Query, schema: Schema, limits: QueryLimits) -> QueryPlan:
    """Validate `query` against `schema`; raise `QueryError` listing every problem."""
    resolver = _Resolver(schema, limits)
    target = resolver.target(query)
    if target is None:
        raise QueryError(resolver.issues)
    where = None if query.where is None else resolver.condition(query.where, target, "/where", 0)
    plan = QueryPlan(
        target=target,
        where=where,
        select=resolver.select(target, query.select, "/select"),
        include=resolver.includes(target, query),
        order=resolver.order(target, query),
        limit=resolver.limit(query),
        cursor=query.cursor,
        aggregate=resolver.aggregate(target, query),
    )
    if resolver.issues:
        raise QueryError(resolver.issues)
    return plan


def resolve_where(
    predicate: Predicate, target: Target, schema: Schema, limits: QueryLimits, pointer: str = ""
) -> Condition:
    """Validate a standalone predicate over `target` (search filters, traversal hops)."""
    resolver = _Resolver(schema, limits)
    condition = resolver.condition(predicate, target, pointer, 0)
    if resolver.issues or condition is None:
        raise QueryError(resolver.issues)
    return condition
