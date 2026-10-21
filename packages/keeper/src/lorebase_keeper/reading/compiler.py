"""Compile resolved DSL plans to SQL over the materialized head of one branch.

Everything comes from the plan, which holds ids and canonical operands; every value, id and
key reaches Postgres as a bound parameter, never as SQL text, except where a per-Attribute
partial index must match: comparisons on an indexed Attribute of one Kind render the index
predicate (Domain, branch, Kind) and the Attribute id as escaped literals and compare the
index expression (`lorebase_store.indexes.IndexSpec`). Attribute values are read from
`values` (or `derived`) by Attribute id and cast per type: integer and decimal to numeric,
float to double precision, date, timestamp and duration to their Postgres types, text types as
text. Equality on scalar Attributes of one Kind uses JSONB containment (`values @> {...}`) so
the GIN index on `values` serves it.

`via` and `count` compile to correlated subqueries over the Links of the Relation. Relations
backed by a foreign key Attribute have no stored Links; their Links are read from the `from`
Records holding the Attribute.

Ordering is keyset based: each ordering term sorts by `(value IS NULL, value)` so that absent
values come last in both directions, then by Kind id (Trait targets) and key, which makes
every position unique. `SortKey` carries what a cursor needs to resume after a row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    ColumnElement,
    Date,
    DateTime,
    FromClause,
    Interval,
    Numeric,
    Select,
    Text,
    and_,
    case,
    cast,
    exists,
    false,
    func,
    literal,
    not_,
    or_,
    over,
    select,
    true,
)
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, JSONB, REGCONFIG
from sqlalchemy.sql.elements import KeyedColumnElement
from sqlalchemy.types import TypeEngine

from lorebase_core.model import LinkDirection
from lorebase_core.query import (
    Comparison,
    Condition,
    Conjunction,
    Disjunction,
    FieldRef,
    FieldSource,
    FullText,
    Hop,
    LinkCount,
    LinkExists,
    Negation,
    Operator,
    Ordering,
    Target,
)
from lorebase_core.types import AttributeType, decode_value
from lorebase_store.indexes import IndexMethod, IndexSpec
from lorebase_store.repositories.materialized import MAT_LINKS, MAT_RECORDS

EARTH_RADIUS_M = 6_371_008.8
"""Mean Earth radius (IUGG) used by `near`."""

_TYPED: dict[AttributeType, TypeEngine[Any]] = {
    AttributeType.INTEGER: Numeric(),
    AttributeType.DECIMAL: Numeric(),
    AttributeType.FLOAT: DOUBLE_PRECISION(),
    AttributeType.BOOLEAN: Boolean(),
    AttributeType.DATE: Date(),
    AttributeType.TIMESTAMP: DateTime(timezone=True),
    AttributeType.DURATION: Interval(),
}
_SCALAR_JSON = frozenset(
    {AttributeType.ARRAY, AttributeType.JSON, AttributeType.GEO_POINT}
)  # compared as JSONB, never through containment of a scalar

_INDEXED_OPERATORS = frozenset(
    {
        Operator.EQ,
        Operator.IN,
        Operator.LT,
        Operator.LTE,
        Operator.GT,
        Operator.GTE,
        Operator.BETWEEN,
    }
)
_RANGE_OPERATORS = frozenset(
    {Operator.LT, Operator.LTE, Operator.GT, Operator.GTE, Operator.BETWEEN}
)
_TEXT_ORDERED = frozenset(
    {AttributeType.STRING, AttributeType.URI, AttributeType.ENUM, AttributeType.REF}
)
"""Text-indexed types this compiler also orders as text; dates, timestamps and durations are
compared as their SQL types, so ranges on them keep the typed comparison."""

_COMPARE = {
    Operator.EQ: lambda a, b: a == b,
    Operator.NE: lambda a, b: a != b,
    Operator.LT: lambda a, b: a < b,
    Operator.LTE: lambda a, b: a <= b,
    Operator.GT: lambda a, b: a > b,
    Operator.GTE: lambda a, b: a >= b,
}


@dataclass(frozen=True, slots=True)
class LinkSource:
    """Rows of one Relation seen from one direction: `near` and `far` key columns."""

    rows: FromClause
    near: ColumnElement[str]
    far: ColumnElement[str]
    values: ColumnElement[Any]
    scope: ColumnElement[bool]


@dataclass(frozen=True, slots=True)
class SortKey:
    """One column of a keyset ordering."""

    expression: ColumnElement[Any]
    descending: bool = False

    @property
    def type(self) -> TypeEngine[Any]:
        return self.expression.type


class Compiler:
    """SQL builder for the materialized head of `branch` in `domain_id`.

    `indexes` are the per-Attribute indexes of the branch schema by `(kind id, attribute
    id)` (`lorebase_store.indexes.plan_indexes`); comparisons on those Attributes repeat the
    index predicate and expression so the partial index can serve them.
    """

    def __init__(
        self,
        domain_id: str,
        branch: str,
        *,
        text_search_config: str,
        indexes: Mapping[tuple[str, str], IndexSpec] | None = None,
    ) -> None:
        self.domain_id = domain_id
        self.branch = branch
        self._config = text_search_config
        self._indexes = dict(indexes or {})

    # Tables and scopes ------------------------------------------------------------------

    @staticmethod
    def records(name: str | None = None) -> FromClause:
        return MAT_RECORDS.alias(name) if name else MAT_RECORDS

    def record_scope(self, rows: FromClause, target: Target) -> ColumnElement[bool]:
        """Rows of the target's Kinds on this branch."""
        kinds = target.kind_ids
        kind = (
            rows.c.kind_id == literal(kinds[0])
            if len(kinds) == 1
            else rows.c.kind_id.in_([literal(k) for k in kinds])
        )
        return and_(
            rows.c.domain_id == literal(self.domain_id),
            rows.c.branch == literal(self.branch),
            kind,
        )

    def regconfig(self) -> ColumnElement[Any]:
        return cast(literal(self._config), REGCONFIG)

    def tsquery(self, text_query: str) -> ColumnElement[Any]:
        return func.websearch_to_tsquery(self.regconfig(), literal(text_query))

    # Fields -----------------------------------------------------------------------------

    def field_json(self, rows: FromClause, ref: FieldRef) -> ColumnElement[Any]:
        """Canonical value of a field as JSONB (`NULL` when absent)."""
        if ref.source is FieldSource.KEY:
            return func.to_jsonb(rows.c.key)
        if ref.source is FieldSource.TITLE:
            return func.to_jsonb(rows.c.title)
        column = rows.c.derived if ref.source is FieldSource.DERIVED else rows.c["values"]
        return self._per_kind(rows, ref, lambda attribute: json_get(column, attribute))

    def field_text(self, rows: FromClause, ref: FieldRef) -> ColumnElement[Any]:
        """Canonical value of a scalar field as text."""
        if ref.source is FieldSource.KEY:
            return rows.c.key
        if ref.source is FieldSource.TITLE:
            return rows.c.title
        column = rows.c.derived if ref.source is FieldSource.DERIVED else rows.c["values"]
        return self._per_kind(rows, ref, lambda attribute: json_text(column, attribute))

    def field_typed(self, rows: FromClause, ref: FieldRef) -> ColumnElement[Any]:
        """Field value cast to the SQL type of its Attribute type (text for text types)."""
        text_value = self.field_text(rows, ref)
        sql_type = _TYPED.get(ref.spec.name)
        return text_value if sql_type is None else cast(text_value, sql_type)

    def _per_kind(self, rows: FromClause, ref: FieldRef, read: Any) -> ColumnElement[Any]:
        items = sorted(ref.attributes.items())
        if not items:
            return literal(None)
        if len(items) == 1:
            return read(items[0][1])  # type: ignore[no-any-return]
        return case(
            *((rows.c.kind_id == literal(kind), read(attribute)) for kind, attribute in items)
        )

    # Conditions -------------------------------------------------------------------------

    def condition(
        self, rows: FromClause, condition: Condition, target: Target
    ) -> ColumnElement[bool]:
        """SQL predicate of a resolved condition over `rows` holding the target Records."""
        match condition:
            case Conjunction(items=items):
                return and_(*(self.condition(rows, c, target) for c in items))
            case Disjunction(items=items):
                return or_(*(self.condition(rows, c, target) for c in items))
            case Negation(item=item):
                return not_(self.condition(rows, item, target))
            case FullText(text=text_query):
                return rows.c.search.op("@@", return_type=Boolean)(self.tsquery(text_query))
            case Comparison():
                return self.comparison(rows, condition, target)
            case LinkExists(hop=hop, where=where):
                source = self.link_source(hop)
                query = select(literal(1)).select_from(source.rows)
                query = query.where(source.scope, source.near == rows.c.key)
                if len(target.kinds) > 1:
                    query = query.where(rows.c.kind_id == literal(hop.near_kind))
                if where is not None:
                    far = MAT_RECORDS.alias()
                    query = query.join(far, self._far_join(far, source, hop)).where(
                        self.condition(far, where, hop.far)
                    )
                return exists(query)
            case LinkCount(hop=hop, op=op, operand=operand, where=where):
                source = self.link_source(hop)
                query = select(func.count()).select_from(source.rows)
                query = query.where(source.scope, source.near == rows.c.key)
                if where is not None:
                    far = MAT_RECORDS.alias()
                    query = query.join(far, self._far_join(far, source, hop)).where(
                        self.condition(far, where, hop.far)
                    )
                counted = query.scalar_subquery()
                compared = _compare(counted, op, operand)
                if len(target.kinds) > 1:
                    return and_(rows.c.kind_id == literal(hop.near_kind), compared)
                return compared
        raise AssertionError(f"unexpected condition {condition!r}")  # pragma: no cover

    def comparison(
        self, rows: FromClause, condition: Comparison, target: Target
    ) -> ColumnElement[bool]:
        ref, op, operand = condition.field, condition.op, condition.operand
        spec = ref.spec
        if op is Operator.EXISTS:
            present = self._present(rows, ref)
            return present if operand else not_(present)
        if op is Operator.PREFIX:
            return self.field_text(rows, ref).startswith(str(operand), autoescape=True)
        if op is Operator.MATCH:
            vector = func.to_tsvector(self.regconfig(), self.field_text(rows, ref))
            return vector.op("@@", return_type=Boolean)(self.tsquery(operand))
        if op is Operator.CONTAINS:
            return self.field_json(rows, ref).contains(_jsonb(list(operand)))
        if op is Operator.NEAR:
            return self._near(rows, ref, operand)
        indexed = self._indexed(rows, condition, target)
        if indexed is not None:
            return indexed
        if (
            op is Operator.EQ
            and ref.source in (FieldSource.VALUE, FieldSource.DERIVED)
            and len(ref.attributes) == 1
            and spec.name not in _SCALAR_JSON
        ):
            column = rows.c.derived if ref.source is FieldSource.DERIVED else rows.c["values"]
            ((_, attribute),) = ref.attributes.items()
            return column.contains(_jsonb({attribute: operand}))
        typed = self.field_typed(rows, ref)
        if op is Operator.IN:
            return typed.in_([_bind(spec, value, typed) for value in operand])
        if op is Operator.BETWEEN:
            low, high = operand
            return typed.between(_bind(spec, low, typed), _bind(spec, high, typed))
        return _compare(typed, op, _bind(spec, operand, typed))

    def _indexed(
        self, rows: FromClause, condition: Comparison, target: Target
    ) -> ColumnElement[bool] | None:
        """A comparison in the shape of the Attribute's partial index, `None` when it has
        no index or the index cannot serve the operator.

        The index predicate (Domain, branch and Kind) and the Attribute id are rendered as
        literals: a partial index matches only a query that repeats its literals. Equality
        adds `IS NOT NULL` so that absent values compare false, as with containment.
        """
        ref, op, operand = condition.field, condition.op, condition.operand
        if ref.source is not FieldSource.VALUE or len(ref.attributes) != 1:
            return None
        ((kind_id, attribute_id),) = ref.attributes.items()
        spec = self._indexes.get((kind_id, attribute_id))
        if spec is None or target.kind_ids != (kind_id,) or op not in _INDEXED_OPERATORS:
            return None
        if op is Operator.IN and spec.method is not IndexMethod.BTREE:
            return None
        if op in _RANGE_OPERATORS and not (
            spec.ordered and (spec.cast is not None or spec.attribute_type in _TEXT_ORDERED)
        ):
            return None
        expression = _index_expression(rows, spec)
        compared: ColumnElement[bool]
        if op is Operator.EQ:
            compared = and_(expression == _index_value(spec, operand), expression.is_not(None))
        elif op is Operator.IN:
            compared = expression.in_([_index_value(spec, value) for value in operand])
        elif op is Operator.BETWEEN:
            low, high = operand
            compared = expression.between(_index_value(spec, low), _index_value(spec, high))
        else:
            compared = _COMPARE[op](expression, _index_value(spec, operand))
        return and_(
            rows.c.domain_id == literal(self.domain_id, Text(), literal_execute=True),
            rows.c.branch == literal(self.branch, Text(), literal_execute=True),
            rows.c.kind_id == literal(kind_id, Text(), literal_execute=True),
            compared,
        )

    def _present(self, rows: FromClause, ref: FieldRef) -> ColumnElement[bool]:
        if ref.source is FieldSource.KEY:
            return true()
        if ref.source is FieldSource.TITLE:
            return rows.c.title.is_not(None)
        column = rows.c.derived if ref.source is FieldSource.DERIVED else rows.c["values"]
        if len(ref.attributes) == 1:
            ((_, attribute),) = ref.attributes.items()
            present: ColumnElement[bool] = column.has_key(attribute)
            return present
        return self.field_json(rows, ref).is_not(None)

    def _near(
        self, rows: FromClause, ref: FieldRef, operand: tuple[float, float, float]
    ) -> ColumnElement[bool]:
        lat, lon, radius = operand
        point = self.field_json(rows, ref)
        rlat = func.radians(cast(json_text(point, "lat"), DOUBLE_PRECISION))
        rlon = func.radians(cast(json_text(point, "lon"), DOUBLE_PRECISION))
        qlat = func.radians(literal(lat, DOUBLE_PRECISION))
        qlon = func.radians(literal(lon, DOUBLE_PRECISION))
        haversine = func.power(func.sin((rlat - qlat) / 2), 2) + func.cos(rlat) * func.cos(
            qlat
        ) * func.power(func.sin((rlon - qlon) / 2), 2)
        distance = literal(2 * EARTH_RADIUS_M, DOUBLE_PRECISION) * func.asin(func.sqrt(haversine))
        return distance <= literal(radius, DOUBLE_PRECISION)

    # Links ------------------------------------------------------------------------------

    def link_source(self, hop: Hop) -> LinkSource:
        """Links of the hop's Relation with `near` on the side the hop starts from."""
        relation = hop.relation
        forward = hop.direction is LinkDirection.FORWARD
        if hop.foreign_key:
            assert relation.foreign_key_attribute is not None
            rows = MAT_RECORDS.alias()
            attribute = relation.foreign_key_attribute
            target_key = json_text(rows.c["values"], attribute)
            scope = and_(
                rows.c.domain_id == literal(self.domain_id),
                rows.c.branch == literal(self.branch),
                rows.c.kind_id == literal(relation.from_kind),
                rows.c["values"].has_key(attribute),
            )
            near, far = (rows.c.key, target_key) if forward else (target_key, rows.c.key)
            return LinkSource(rows, near, far, _jsonb({}), scope)
        links = MAT_LINKS.alias()
        scope = and_(
            links.c.domain_id == literal(self.domain_id),
            links.c.branch == literal(self.branch),
            links.c.relation_id == literal(relation.id),
        )
        near, far = (
            (links.c.from_key, links.c.to_key) if forward else (links.c.to_key, links.c.from_key)
        )
        return LinkSource(links, near, far, links.c["values"], scope)

    def _far_join(self, far: FromClause, source: LinkSource, hop: Hop) -> ColumnElement[bool]:
        return and_(
            far.c.domain_id == literal(self.domain_id),
            far.c.branch == literal(self.branch),
            far.c.kind_id == literal(hop.far.kind_ids[0]),
            far.c.key == source.far,
        )

    def neighbors(
        self,
        hop: Hop,
        near_keys: Sequence[str],
        *,
        where: Condition | None = None,
        per_near: int | None = None,
        limit: int | None = None,
    ) -> Select[Any]:
        """Links from `near_keys` along `hop` joined to the far Records.

        Columns: `near_key`, `far_key`, `link_values` and the far Record columns. `per_near`
        keeps the first Links (by far key) of each near key; `limit` caps the whole result.
        """
        source = self.link_source(hop)
        far = MAT_RECORDS.alias("far")
        columns: list[ColumnElement[Any]] = [
            source.near.label("near_key"),
            source.far.label("far_key"),
            source.values.label("link_values"),
            *record_columns(far),
        ]
        if per_near is not None:
            columns.append(
                over(func.row_number(), partition_by=source.near, order_by=source.far).label(
                    "position"
                )
            )
        query = (
            select(*columns)
            .select_from(source.rows)
            .join(far, self._far_join(far, source, hop))
            .where(source.scope, source.near.in_([literal(k) for k in near_keys]))
        )
        if where is not None:
            query = query.where(self.condition(far, where, hop.far))
        if per_near is not None:
            inner = query.subquery("ranked")
            query = select(*(c for c in inner.c if c.name != "position")).where(
                inner.c.position <= literal(per_near)
            )
            query = query.order_by(inner.c.near_key, inner.c.far_key)
        else:
            query = query.order_by(source.near, source.far)
        if limit is not None:
            query = query.limit(limit)
        return query

    # Ordering ---------------------------------------------------------------------------

    def sort_keys(
        self, rows: FromClause, order: Sequence[Ordering], target: Target
    ) -> list[SortKey]:
        """Keyset columns: per term the absence flag and the value, then Kind and key."""
        keys: list[SortKey] = []
        for term in order:
            if term.field.source is FieldSource.KEY:
                keys.append(SortKey(rows.c.key, term.descending))
                continue
            typed = self.field_typed(rows, term.field)
            keys.append(SortKey(typed.is_(None)))
            keys.append(SortKey(typed, term.descending))
        if len(target.kinds) > 1:
            keys.append(SortKey(rows.c.kind_id))
        if not any(k.expression is rows.c.key for k in keys):
            keys.append(SortKey(rows.c.key))
        return keys


def json_get(column: ColumnElement[Any], key: str) -> ColumnElement[Any]:
    """`column -> key` with the key bound as a parameter (JSONB result)."""
    return column.op("->", return_type=JSONB)(literal(key, Text()))


def json_text(column: ColumnElement[Any], key: str) -> ColumnElement[Any]:
    """`column ->> key` with the key bound as a parameter (text result)."""
    return column.op("->>", return_type=Text)(literal(key, Text()))


def record_columns(rows: FromClause) -> list[KeyedColumnElement[Any]]:
    """Columns of a materialized Record read by the API."""
    return [
        rows.c.kind_id,
        rows.c.key,
        rows.c.title,
        rows.c["values"],
        rows.c.derived,
        rows.c.object_hash,
        rows.c.lineage,
        rows.c.updated_revision,
        rows.c.updated_at,
    ]


def order_clauses(keys: Sequence[SortKey]) -> list[ColumnElement[Any]]:
    return [k.expression.desc() if k.descending else k.expression.asc() for k in keys]


def after(keys: Sequence[SortKey], values: Sequence[Any]) -> ColumnElement[bool]:
    """Rows strictly after the position `values` in the keyset order of `keys`."""
    if len(keys) != len(values):
        raise ValueError("cursor does not match the ordering")
    alternatives: list[ColumnElement[bool]] = []
    equal: list[ColumnElement[bool]] = []
    for key, value in zip(keys, values, strict=True):
        if value is None:
            beyond: ColumnElement[bool] = false()
            same: ColumnElement[bool] = key.expression.is_(None)
        else:
            bound = literal(value, key.type)
            beyond = key.expression < bound if key.descending else key.expression > bound
            same = key.expression == bound
        alternatives.append(and_(*equal, beyond) if equal else beyond)
        equal.append(same)
    return or_(*alternatives)


def _compare(left: ColumnElement[Any], op: Operator, right: Any) -> ColumnElement[bool]:
    if op is Operator.BETWEEN:
        low, high = right
        return left.between(literal(low), literal(high))
    if not isinstance(right, ColumnElement):
        right = literal(right)
    result: ColumnElement[bool] = _COMPARE[op](left, right)
    return result


_INDEX_CASTS: dict[str, TypeEngine[Any]] = {
    "bigint": BigInteger(),
    "numeric": Numeric(),
    "double precision": DOUBLE_PRECISION(),
    "boolean": Boolean(),
}


def _index_expression(rows: FromClause, spec: IndexSpec) -> ColumnElement[Any]:
    """`spec.expression` over `rows`, with the Attribute id as a literal."""
    raw = rows.c["values"].op("->>", return_type=Text)(
        literal(spec.attribute_id, Text(), literal_execute=True)
    )
    if spec.digest:
        return func.md5(raw)
    if spec.cast is not None:
        return cast(raw, _INDEX_CASTS[spec.cast])
    return raw


def _index_value(spec: IndexSpec, canonical: Any) -> ColumnElement[Any]:
    """A canonical operand compared with `_index_expression` (`spec.value_sql`)."""
    if isinstance(canonical, bool):
        canonical = "true" if canonical else "false"
    value = literal(str(canonical), Text())
    if spec.digest:
        return func.md5(value)
    if spec.cast is not None:
        return cast(value, _INDEX_CASTS[spec.cast])
    return value


def _jsonb(value: Any) -> ColumnElement[Any]:
    return literal(value, JSONB)


def _bind(spec: Any, canonical: Any, typed: ColumnElement[Any]) -> ColumnElement[Any]:
    """A canonical operand as a parameter of the field's SQL type."""
    value = decode_value(spec, canonical) if spec.name in _TYPED else canonical
    return literal(value, typed.type if spec.name in _TYPED else Text())


def cursor_value(value: Any) -> Any:
    """A sort value as JSON for a cursor (see `from_cursor_value`)."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds
    return value


def from_cursor_value(value: Any, sql_type: TypeEngine[Any]) -> Any:
    """Inverse of `cursor_value` for a column of `sql_type`; `ValueError` when malformed."""
    if value is None:
        return None
    try:
        if isinstance(sql_type, Boolean):
            if not isinstance(value, bool):
                raise ValueError("expected a boolean")
            return value
        if isinstance(sql_type, Numeric) and not isinstance(sql_type, DOUBLE_PRECISION):
            return Decimal(str(value))
        if isinstance(sql_type, DOUBLE_PRECISION):
            return float(value)
        if isinstance(sql_type, DateTime):
            return datetime.fromisoformat(str(value))
        if isinstance(sql_type, Date):
            return date.fromisoformat(str(value))
        if isinstance(sql_type, Interval):
            return timedelta(microseconds=int(value))
    except (ArithmeticError, TypeError) as error:
        raise ValueError(f"malformed cursor value: {error}") from None
    if not isinstance(value, str):
        raise ValueError("expected text")
    return value
