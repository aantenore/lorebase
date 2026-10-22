"""Row mapping of a Source (design 5.9): transforms, type coercion, keys and Link targets.

A `RowMapper` is compiled once per run from the schema and the Source, which also checks
everything a single row cannot fix (unknown columns are a row problem, an unknown Relation is
a definition problem). `map` turns one source row into a `MappedRow`:

- transforms run in order on the column value: `trim`, `lower`, `upper`, `null_if`
  (`value` or `values`), `constant` (`value`), `concat` (`columns`, `separator`), `map`
  (`values`, optional `default`), `parse_date` (`format`, strptime syntax) and `cast` (`to`:
  `string`, `integer`, `decimal`, `float`, `boolean`, `date` or `timestamp`);
- the result is coerced to the Attribute type through the core encoding: text is cast from
  `string` with the core cast rules, native values (numbers, dates, booleans from Postgres)
  are encoded directly, empty text is an absent value and naive timestamps are read as UTC;
- the key comes from `key_mapping`, one column per primary key Attribute, coerced to that
  Attribute's type; the primary key Attributes are set from it;
- each Relation mapping names the columns holding the primary key of the `to` Record.

An Attribute mapped by `expression` instead of `column` takes the value of a CEL expression over
the row (`lorebase_core.expr.rows`: columns are variables, `row` holds them all); the transforms
and the coercion then apply to that value like to a column's. Expressions are compiled once over
the Source's columns (`use_columns`: the header, or every key of the rows read); a column a row
lacks is null.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import JsonValue

from lorebase_core.expr import ExprError, ExprLimits, RowExpressions
from lorebase_core.model import (
    Attribute,
    Kind,
    Record,
    Relation,
    RelationBacking,
    Schema,
    Source,
    record_key,
)
from lorebase_core.model.source import Transform, TransformName
from lorebase_core.types import AttributeType, TypeSpec, TypeSystemError, cast, encode_value
from lorebase_core.types.encoding import format_date, format_timestamp

Row = Mapping[str, Any]
"""One source row: column name to raw value (text for CSV, native values for Postgres)."""

_TEXTUAL = frozenset(
    {
        AttributeType.STRING,
        AttributeType.TEXT,
        AttributeType.URI,
        AttributeType.ENUM,
        AttributeType.REF,
    }
)
_STRING = TypeSpec.of(AttributeType.STRING)
_TRUE = frozenset({"true", "t", "yes", "y", "1"})
_FALSE = frozenset({"false", "f", "no", "n", "0"})
_CAST_TARGETS = frozenset(
    {"string", "text", "integer", "decimal", "float", "boolean", "date", "timestamp"}
)


class SourceDefinitionError(ValueError):
    """The Source cannot be synchronized as defined; no row can fix it."""


class RowError(ValueError):
    """One row cannot be mapped."""

    def __init__(self, locator: str, message: str, *, column: str | None = None) -> None:
        super().__init__(message)
        self.locator = locator
        self.column = column

    def as_dict(self) -> dict[str, JsonValue]:
        return {"row": self.locator, "column": self.column, "message": str(self)}


@dataclass(frozen=True)
class MappedRow:
    """A mapped row: Record key, canonical values of the Attributes the Source owns, and the
    `to` keys of each mapped Relation (an empty set: no Link)."""

    key: str
    values: dict[str, JsonValue]
    links: dict[str, frozenset[str]] = field(default_factory=dict)
    locator: str = ""
    tombstone: bool = False


def text_of(value: Any) -> str:
    """Text form of a native value, used for textual Attributes and string transforms."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return format_timestamp(value if value.tzinfo else value.replace(tzinfo=UTC))
    if isinstance(value, date):
        return format_date(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def coerce(value: Any, spec: TypeSpec) -> JsonValue:
    """Canonical value of `spec` for a raw value; raises `TypeSystemError` or `ValueError`."""
    if _is_empty(value):
        return None
    if spec.name in _TEXTUAL:
        return encode_value(spec, text_of(value))
    if isinstance(value, str):
        if spec.name is AttributeType.JSON:
            return encode_value(spec, json.loads(value))
        return cast(_STRING, spec, value.strip())
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    if isinstance(value, float) and spec.name is AttributeType.DECIMAL:
        value = Decimal(repr(value))
    return encode_value(spec, value)


def _cast_native(value: Any, target: str) -> Any:
    if target in ("string", "text"):
        return text_of(value)
    text = text_of(value).strip() if not isinstance(value, int | float | Decimal) else None
    if target == "integer":
        if text is None:
            number = Decimal(str(value))
            if number != number.to_integral_value():
                raise ValueError(f"{value} is not an integer")
            return int(number)
        return int(Decimal(text)) if "." in text else int(text)
    if target == "decimal":
        return Decimal(text if text is not None else str(value))
    if target == "float":
        return float(text if text is not None else value)
    if target == "boolean":
        if isinstance(value, bool):
            return value
        lowered = text_of(value).strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise ValueError(f"not a boolean: {value!r}")
    if target == "date":
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(text_of(value).strip())
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(text_of(value).strip())
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _check_transform(transform: Transform, where: str) -> None:
    args = transform.args
    name = transform.name
    if name is TransformName.CAST and args.get("to") not in _CAST_TARGETS:
        raise SourceDefinitionError(
            f"{where}: cast needs `to`, one of {', '.join(sorted(_CAST_TARGETS))}"
        )
    if name is TransformName.PARSE_DATE and not isinstance(args.get("format"), str):
        raise SourceDefinitionError(f"{where}: parse_date needs a `format`")
    if name is TransformName.MAP and not isinstance(args.get("values"), dict):
        raise SourceDefinitionError(f"{where}: map needs `values` (an object)")
    if name is TransformName.CONSTANT and "value" not in args:
        raise SourceDefinitionError(f"{where}: constant needs a `value`")
    if name is TransformName.NULL_IF and "value" not in args and "values" not in args:
        raise SourceDefinitionError(f"{where}: null_if needs `value` or `values`")
    if name is TransformName.CONCAT and not isinstance(args.get("columns"), list):
        raise SourceDefinitionError(f"{where}: concat needs `columns` (a list)")


def apply_transform(transform: Transform, value: Any, row: Row) -> Any:
    """One transform on a value of `row` (pure)."""
    args = transform.args
    name = transform.name
    if name is TransformName.CONSTANT:
        return args["value"]
    if name is TransformName.CONCAT:
        separator = str(args.get("separator", ""))
        columns = args.get("columns")
        assert isinstance(columns, list)
        parts = [value, *(row.get(str(c)) for c in columns)]
        return separator.join(text_of(p) for p in parts if not _is_empty(p))
    if value is None:
        return None
    if name is TransformName.TRIM:
        return value.strip() if isinstance(value, str) else value
    if name is TransformName.LOWER:
        return text_of(value).lower()
    if name is TransformName.UPPER:
        return text_of(value).upper()
    if name is TransformName.NULL_IF:
        candidates = args.get("values", [args.get("value")])
        assert isinstance(candidates, list)
        return None if text_of(value) in {text_of(c) for c in candidates} else value
    if name is TransformName.MAP:
        values = args["values"]
        assert isinstance(values, dict)
        found = values.get(text_of(value))
        if found is not None:
            return found
        return args.get("default", value)
    if name is TransformName.PARSE_DATE:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return datetime.strptime(text_of(value).strip(), str(args["format"])).date()
    return _cast_native(value, str(args["to"]))


@dataclass(frozen=True)
class _AttributeRule:
    attribute: Attribute
    column: str | None
    expression: str | None
    transforms: tuple[Transform, ...]


@dataclass(frozen=True)
class _LinkRule:
    relation: Relation
    columns: tuple[str, ...]
    to_kind: Kind


class RowMapper:
    """Maps rows of one Source onto its target Kind."""

    def __init__(self, schema: Schema, source: Source, *, limits: ExprLimits | None = None) -> None:
        self._expressions = RowExpressions(schema, limits=limits)
        kind = schema.kinds.get(source.target_kind)
        if kind is None:
            raise SourceDefinitionError(f"unknown target Kind {source.target_kind}")
        if not kind.has_natural_key:
            raise SourceDefinitionError(f"Kind {kind.api_name!r} has no primary key")
        if len(source.key_mapping) != len(kind.primary_key):
            raise SourceDefinitionError(
                f"key_mapping has {len(source.key_mapping)} columns, the primary key of "
                f"{kind.api_name!r} has {len(kind.primary_key)}"
            )
        self.source = source
        self.kind = kind
        self._keys = [
            (kind.attributes[attribute_id], column)
            for attribute_id, column in zip(kind.primary_key, source.key_mapping, strict=True)
        ]
        self._rules = self._attribute_rules(kind, source)
        self._links = self._link_rules(schema, kind, source)

    @staticmethod
    def _attribute_rules(kind: Kind, source: Source) -> list[_AttributeRule]:
        rules = []
        for attribute_id, mapping in source.attribute_mapping.items():
            attribute = kind.attributes.get(attribute_id)
            if attribute is None:
                raise SourceDefinitionError(f"{attribute_id} is not an Attribute of the Kind")
            if attribute.is_derived:
                raise SourceDefinitionError(f"derived Attribute {attribute.api_name!r} mapped")
            if attribute_id in kind.primary_key:
                continue  # primary key Attributes come from key_mapping
            for transform in mapping.transforms:
                _check_transform(transform, attribute.api_name)
            rules.append(
                _AttributeRule(attribute, mapping.column, mapping.expression, mapping.transforms)
            )
        return rules

    @staticmethod
    def _link_rules(schema: Schema, kind: Kind, source: Source) -> list[_LinkRule]:
        rules = []
        for mapping in source.relation_mapping:
            relation = schema.relations.get(mapping.relation)
            if relation is None:
                raise SourceDefinitionError(f"unknown Relation {mapping.relation}")
            if relation.backing is not RelationBacking.EXPLICIT:
                raise SourceDefinitionError(
                    f"Relation {relation.api_name!r} derives its Links from an Attribute; map "
                    "the Attribute instead"
                )
            if relation.from_kind != kind.id:
                raise SourceDefinitionError(
                    f"Relation {relation.api_name!r} does not start at Kind {kind.api_name!r}"
                )
            to_kind = schema.kinds.get(relation.to_kind)
            if to_kind is None or not to_kind.has_natural_key:
                raise SourceDefinitionError(
                    f"the target Kind of Relation {relation.api_name!r} has no primary key"
                )
            if len(mapping.columns) != len(to_kind.primary_key):
                raise SourceDefinitionError(
                    f"Relation {relation.api_name!r}: {len(mapping.columns)} columns for a "
                    f"primary key of {len(to_kind.primary_key)}"
                )
            rules.append(_LinkRule(relation, mapping.columns, to_kind))
        return rules

    @property
    def owned_attributes(self) -> frozenset[str]:
        """Attributes the Source writes: its mapped ones and the primary key."""
        return frozenset([*(rule.attribute.id for rule in self._rules), *self.kind.primary_key])

    @property
    def relations(self) -> tuple[str, ...]:
        """Ids of the mapped Relations."""
        return tuple(rule.relation.id for rule in self._links)

    @property
    def columns(self) -> frozenset[str]:
        """Every column the mapping reads."""
        names = {column for _, column in self._keys}
        names.update(rule.column for rule in self._rules if rule.column is not None)
        for rule in self._rules:
            for transform in rule.transforms:
                if transform.name is TransformName.CONCAT:
                    extra = transform.args.get("columns")
                    assert isinstance(extra, list)
                    names.update(str(c) for c in extra)
        for link in self._links:
            names.update(link.columns)
        return frozenset(names)

    @property
    def has_expressions(self) -> bool:
        """Whether an Attribute is mapped by an expression."""
        return any(rule.expression is not None for rule in self._rules)

    def use_columns(self, columns: Sequence[str]) -> None:
        """Declare the Source's columns, which expressions are compiled over. Columns of a row
        not declared yet are added when it is mapped."""
        self._expressions.declare(columns)

    def check_columns(self, available: Sequence[str]) -> None:
        """Refuse a source whose header lacks columns the mapping reads."""
        missing = sorted(self.columns - set(available))
        if missing:
            raise SourceDefinitionError(f"columns missing from the source: {', '.join(missing)}")

    def _key(self, row: Row, locator: str) -> tuple[str, dict[str, JsonValue]]:
        values: dict[str, JsonValue] = {}
        for attribute, column in self._keys:
            raw = row.get(column)
            if _is_empty(raw):
                raise RowError(locator, f"key column {column!r} is empty", column=column)
            try:
                values[attribute.id] = coerce(
                    raw.strip() if isinstance(raw, str) else raw, attribute.type
                )
            except (TypeSystemError, ValueError) as error:
                raise RowError(locator, f"key {column!r}: {error}", column=column) from None
        key = record_key(self.kind, values)
        if key is None:
            raise RowError(locator, "the key cannot be derived")
        return key, values

    def _input(self, rule: _AttributeRule, row: Row) -> Any:
        """The value a rule starts from: its column, or the expression over the row."""
        if rule.expression is None:
            return row.get(str(rule.column))
        return self._expressions.evaluate(rule.expression, row, now=datetime.now(UTC))

    def map(self, row: Row, locator: str) -> MappedRow:
        """Map one row; raises `RowError` when a value does not fit."""
        key, values = self._key(row, locator)
        tombstone_column = self.source.tombstone_column
        if tombstone_column is not None and not _is_empty(row.get(tombstone_column)):
            return MappedRow(key=key, values=values, locator=locator, tombstone=True)
        for rule in self._rules:
            try:
                value = self._input(rule, row)
                for transform in rule.transforms:
                    value = apply_transform(transform, value, row)
                values[rule.attribute.id] = coerce(value, rule.attribute.type)
            except (
                TypeSystemError,
                ValueError,
                InvalidOperation,
                ArithmeticError,
                ExprError,
            ) as error:
                raise RowError(
                    locator, f"{rule.attribute.api_name}: {error}", column=rule.column
                ) from None
        links: dict[str, frozenset[str]] = {}
        for link in self._links:
            raw = [row.get(column) for column in link.columns]
            if all(_is_empty(v) for v in raw):
                links[link.relation.id] = frozenset()
                continue
            if any(_is_empty(v) for v in raw):
                raise RowError(locator, f"{link.relation.api_name}: incomplete target key")
            target: dict[str, JsonValue] = {}
            try:
                for attribute_id, value in zip(link.to_kind.primary_key, raw, strict=True):
                    target[attribute_id] = coerce(value, link.to_kind.attributes[attribute_id].type)
            except (TypeSystemError, ValueError) as error:
                raise RowError(locator, f"{link.relation.api_name}: {error}") from None
            to_key = record_key(link.to_kind, target)
            if to_key is None:
                raise RowError(locator, f"{link.relation.api_name}: no target key")
            links[link.relation.id] = frozenset({to_key})
        return MappedRow(key=key, values=values, links=links, locator=locator)

    def record(self, mapped: MappedRow, current: Mapping[str, JsonValue] | None) -> Record:
        """The new Record: values of Attributes the Source does not own are kept."""
        owned = self.owned_attributes
        kept = {a: v for a, v in (current or {}).items() if a not in owned}
        fresh = {a: v for a, v in mapped.values.items() if v is not None}
        return Record(kind=self.kind.id, key=mapped.key, values={**kept, **fresh})
