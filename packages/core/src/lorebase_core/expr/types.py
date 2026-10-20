"""Static types of expressions and their mapping from Lorebase Attribute types.

CEL types plus three Lorebase additions: ``decimal`` (exact arithmetic), ``date`` and the
schema types ``ref(Kind)`` (a Record reference) and ``record(Kind)`` (a loaded Record whose
fields are the Kind's Attributes). Objects (``params``, ``principal``, ``links``) are named
types whose fields the Environment declares.

String types may carry ``values``: the closed set of strings they can hold, from an enum
Attribute or from string literals. It lets the checker reject comparisons that can never
be true and values that do not fit an enum.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from lorebase_core.types import AttributeType, TypeSpec


class TypeKind(StrEnum):
    BOOL = "bool"
    INT = "int"
    DOUBLE = "double"
    DECIMAL = "decimal"
    STRING = "string"
    BYTES = "bytes"
    NULL = "null"
    TIMESTAMP = "timestamp"
    DURATION = "duration"
    DATE = "date"
    LIST = "list"
    MAP = "map"
    REF = "ref"
    RECORD = "record"
    OBJECT = "object"
    DYN = "dyn"


#: Kinds with a numeric value; comparisons across them are allowed (CEL heterogeneous
#: numeric comparison).
NUMERIC = frozenset({TypeKind.INT, TypeKind.DOUBLE, TypeKind.DECIMAL})

#: Kinds with a total order (besides the numeric ones).
ORDERED = frozenset(
    {
        TypeKind.STRING,
        TypeKind.BYTES,
        TypeKind.BOOL,
        TypeKind.TIMESTAMP,
        TypeKind.DURATION,
        TypeKind.DATE,
    }
)


@dataclass(frozen=True, slots=True)
class ExprType:
    """A static type. ``name`` is the Kind id of ``ref`` and ``record`` types and the name of
    ``object`` types; ``args`` are the element type of a list and the key and value types of
    a map; ``values`` and ``enum`` describe closed string types (see the module docstring).
    """

    kind: TypeKind
    args: tuple[ExprType, ...] = ()
    name: str | None = None
    values: frozenset[str] | None = None
    enum: bool = False

    def describe(self, names: Callable[[str], str] | None = None) -> str:
        """Readable form; ``names`` maps Kind ids to api names."""
        label = names or (lambda kind_id: kind_id)
        if self.kind is TypeKind.LIST:
            return f"list({self.args[0].describe(names)})"
        if self.kind is TypeKind.MAP:
            return f"map({self.args[0].describe(names)}, {self.args[1].describe(names)})"
        if self.kind in (TypeKind.REF, TypeKind.RECORD):
            return f"{self.kind.value}({label(self.name or '?')})"
        if self.kind is TypeKind.OBJECT:
            return self.name or "object"
        if self.kind is TypeKind.STRING and self.enum and self.values is not None:
            return f"enum({', '.join(sorted(self.values))})"
        return self.kind.value

    @property
    def element(self) -> ExprType:
        """Element type of a list, key type of a map, ``dyn`` otherwise."""
        return self.args[0] if self.args else DYN

    @property
    def is_numeric(self) -> bool:
        return self.kind in NUMERIC

    def plain(self) -> ExprType:
        """The type without a closed value set (for results of string operations)."""
        if self.values is None:
            return self
        return ExprType(self.kind, self.args, self.name)


BOOL = ExprType(TypeKind.BOOL)
INT = ExprType(TypeKind.INT)
DOUBLE = ExprType(TypeKind.DOUBLE)
DECIMAL = ExprType(TypeKind.DECIMAL)
STRING = ExprType(TypeKind.STRING)
BYTES = ExprType(TypeKind.BYTES)
NULL = ExprType(TypeKind.NULL)
TIMESTAMP = ExprType(TypeKind.TIMESTAMP)
DURATION = ExprType(TypeKind.DURATION)
DATE = ExprType(TypeKind.DATE)
DYN = ExprType(TypeKind.DYN)


def list_of(element: ExprType) -> ExprType:
    return ExprType(TypeKind.LIST, (element,))


def map_of(key: ExprType, value: ExprType) -> ExprType:
    return ExprType(TypeKind.MAP, (key, value))


def ref_of(kind_id: str) -> ExprType:
    return ExprType(TypeKind.REF, name=kind_id)


def record_of(kind_id: str) -> ExprType:
    return ExprType(TypeKind.RECORD, name=kind_id)


def object_of(name: str) -> ExprType:
    return ExprType(TypeKind.OBJECT, name=name)


def string_values(values: frozenset[str], *, enum: bool) -> ExprType:
    return ExprType(TypeKind.STRING, values=values, enum=enum)


#: Object type of ``geo_point`` values.
GEO_POINT = "geo_point"

_SCALARS: dict[AttributeType, ExprType] = {
    AttributeType.STRING: STRING,
    AttributeType.TEXT: STRING,
    AttributeType.URI: STRING,
    AttributeType.INTEGER: INT,
    AttributeType.DECIMAL: DECIMAL,
    AttributeType.FLOAT: DOUBLE,
    AttributeType.BOOLEAN: BOOL,
    AttributeType.DATE: DATE,
    AttributeType.TIMESTAMP: TIMESTAMP,
    AttributeType.DURATION: DURATION,
    AttributeType.JSON: DYN,
    AttributeType.GEO_POINT: object_of(GEO_POINT),
}


def from_type_spec(spec: TypeSpec) -> ExprType:
    """The expression type of values of an Attribute (or parameter) of type ``spec``."""
    if spec.name is AttributeType.ARRAY:
        assert spec.element is not None
        return list_of(from_type_spec(spec.element))
    if spec.name is AttributeType.ENUM:
        assert spec.enum_values is not None
        return string_values(frozenset(spec.enum_values), enum=True)
    if spec.name is AttributeType.REF:
        assert spec.kind_ref is not None
        return ref_of(spec.kind_ref)
    return _SCALARS[spec.name]


def assignable(source: ExprType, target: ExprType, *, nullable: bool) -> bool:
    """Whether a value of type ``source`` can be used where ``target`` is expected.

    ``int`` widens to ``decimal`` and ``double``, ``decimal`` to ``double``, and a ``string``
    is accepted as a Record key where a ``ref`` is expected. Closed string types must fit
    the target's value set; open strings are checked when the value is encoded.
    """
    if source.kind is TypeKind.DYN or target.kind is TypeKind.DYN:
        return True
    if source.kind is TypeKind.NULL:
        return nullable
    if source.kind is TypeKind.INT and target.kind in (TypeKind.DECIMAL, TypeKind.DOUBLE):
        return True
    if source.kind is TypeKind.DECIMAL and target.kind is TypeKind.DOUBLE:
        return True
    if source.kind is TypeKind.STRING and target.kind is TypeKind.REF:
        return True
    if source.kind is not target.kind:
        return False
    if source.kind is TypeKind.STRING:
        if target.values is None or source.values is None:
            return True
        return source.values <= target.values
    if source.kind in (TypeKind.REF, TypeKind.RECORD, TypeKind.OBJECT):
        return source.name == target.name
    if source.kind in (TypeKind.LIST, TypeKind.MAP):
        return all(
            assignable(s, t, nullable=False) for s, t in zip(source.args, target.args, strict=True)
        )
    return True


def join(first: ExprType, second: ExprType) -> ExprType:
    """The type of a value that is either ``first`` or ``second`` (branches, list items)."""
    if first == second:
        return first
    if first.kind is TypeKind.NULL:
        return second
    if second.kind is TypeKind.NULL:
        return first
    if first.kind is TypeKind.DYN or second.kind is TypeKind.DYN:
        return DYN
    if first.kind is TypeKind.STRING and second.kind is TypeKind.STRING:
        if first.values is None or second.values is None:
            return STRING
        return string_values(first.values | second.values, enum=first.enum and second.enum)
    if first.is_numeric and second.is_numeric:
        kinds = {first.kind, second.kind}
        if TypeKind.DOUBLE in kinds:
            return DOUBLE
        return DECIMAL
    if first.kind is second.kind and first.kind in (TypeKind.LIST, TypeKind.MAP):
        return ExprType(
            first.kind, tuple(join(a, b) for a, b in zip(first.args, second.args, strict=True))
        )
    return DYN
