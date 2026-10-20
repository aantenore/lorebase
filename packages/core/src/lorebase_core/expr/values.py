"""Runtime values of evaluation and their conversion from and to canonical values.

``int`` is int, ``double`` float, ``decimal`` Decimal, ``timestamp`` an aware datetime in
UTC, ``date`` date, ``duration`` timedelta, lists are lists and maps dicts. Schema values
use the classes below. An absent Attribute reads as ``None`` (null).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import JsonValue

from lorebase_core.expr.errors import ExprError, ExprErrorCode, evaluation_error
from lorebase_core.model.kind import Kind
from lorebase_core.model.record import format_ref
from lorebase_core.types import (
    AttributeType,
    EncodingError,
    GeoPoint,
    TypeSpec,
    decode_value,
    encode_value,
)


@dataclass(frozen=True, slots=True)
class Ref:
    """A Record reference: Kind id and key."""

    kind: str
    key: str


@dataclass(frozen=True, slots=True)
class ObjectValue:
    """A value of an object type (``params``, ``principal``)."""

    name: str
    fields: Mapping[str, object]


@dataclass(eq=False, slots=True)
class RecordValue:
    """A loaded Record. Attribute values are decoded on first access."""

    kind: Kind
    key: str
    values: Mapping[str, JsonValue]
    _decoded: dict[str, object] = field(default_factory=dict)

    @property
    def ref(self) -> Ref:
        return Ref(self.kind.id, self.key)

    def has(self, name: str) -> bool:
        attribute = self.kind.attribute_by_api_name(name)
        return attribute is not None and self.values.get(attribute.id) is not None

    def get(self, name: str) -> object:
        """Decoded value of the Attribute called ``name`` (``None`` when absent)."""
        if name in self._decoded:
            return self._decoded[name]
        attribute = self.kind.attribute_by_api_name(name)
        if attribute is None:
            raise evaluation_error(f"Kind {self.kind.api_name} has no attribute {name!r}")
        value = to_runtime(attribute.type, self.values.get(attribute.id))
        self._decoded[name] = value
        return value

    def __repr__(self) -> str:
        return f"RecordValue({format_ref(self.kind.api_name, self.key)})"


@dataclass(eq=False, slots=True)
class LinksValue:
    """``links`` of the subject Record; fields are resolved lazily by the evaluator."""

    kind: Kind
    key: str


def to_runtime(spec: TypeSpec, canonical: object) -> object:
    """Runtime value of a canonical value of ``spec`` (``None`` stays ``None``)."""
    if canonical is None:
        return None
    try:
        value = decode_value(spec, canonical)
    except EncodingError as error:
        raise evaluation_error(f"stored value is invalid: {error.message}") from None
    return _wrap(spec, value)


def _wrap(spec: TypeSpec, value: object) -> object:
    if spec.name is AttributeType.REF:
        assert spec.kind_ref is not None
        assert isinstance(value, str)
        return Ref(spec.kind_ref, value)
    if spec.name is AttributeType.ARRAY and isinstance(value, list):
        assert spec.element is not None
        element = spec.element
        return [_wrap(element, item) for item in value]
    return value


def to_raw(value: object) -> object:
    """A runtime value as input for ``encode_value`` (Record references become keys)."""
    if isinstance(value, Ref):
        return value.key
    if isinstance(value, RecordValue):
        return value.key
    if isinstance(value, list):
        return [to_raw(item) for item in value]
    if isinstance(value, dict):
        return {str(k): to_raw(v) for k, v in value.items()}
    if isinstance(value, ObjectValue):
        return {k: to_raw(v) for k, v in value.fields.items()}
    return value


def encode_result(spec: TypeSpec, value: object) -> JsonValue:
    """Encode an evaluation result for an Attribute of type ``spec``.

    Raises ``ExprError`` with code ``expression.value_invalid`` when the value does not fit.
    """
    if isinstance(value, Ref) and spec.name is AttributeType.REF and value.kind != spec.kind_ref:
        raise ExprError(ExprErrorCode.VALUE_INVALID, "reference to a Record of another Kind")
    try:
        return encode_value(spec, to_raw(value))
    except EncodingError as error:
        raise ExprError(ExprErrorCode.VALUE_INVALID, error.message) from None


def type_name(value: object) -> str:
    """CEL name of the type of a runtime value, for error messages."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "double"
    if isinstance(value, Decimal):
        return "decimal"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bytes):
        return "bytes"
    if isinstance(value, datetime):
        return "timestamp"
    if isinstance(value, date):
        return "date"
    if isinstance(value, timedelta):
        return "duration"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "map"
    if isinstance(value, Ref):
        return "ref"
    if isinstance(value, RecordValue):
        return "record"
    if isinstance(value, GeoPoint):
        return "geo_point"
    return "object"
