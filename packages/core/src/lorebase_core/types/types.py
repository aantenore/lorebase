"""Attribute types (design 5.4): base types and parameterized type specifications."""

from __future__ import annotations

import unicodedata
from enum import StrEnum
from typing import NamedTuple, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

#: Canonical value as stored in Record and Link content (design 6.1): JSON-compatible.
CanonicalValue = JsonValue

#: ``integer`` is a signed 64-bit integer (design 5.4).
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

#: Integers inside ``json`` values must be exactly representable as IEEE 754 doubles
#: so that every JSON implementation reads the same number (I-JSON, RFC 7493).
JSON_SAFE_INTEGER_MAX = 2**53 - 1

#: Prefix and pattern of Kind ids; repeated here so the type system does not depend on
#: the model package (the model package validates the same shape).
KIND_ID_PATTERN = r"^knd_[0-7][0-9A-HJKMNP-TV-Z]{25}$"


class AttributeType(StrEnum):
    """Base types of the Lorebase type system."""

    STRING = "string"
    TEXT = "text"
    URI = "uri"
    ENUM = "enum"
    INTEGER = "integer"
    DECIMAL = "decimal"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"
    DURATION = "duration"
    JSON = "json"
    GEO_POINT = "geo_point"
    REF = "ref"
    ARRAY = "array"


#: Types whose values are text in canonical form.
TEXTUAL_TYPES = frozenset(
    {AttributeType.STRING, AttributeType.TEXT, AttributeType.URI, AttributeType.REF}
)

#: Types with a total order usable by ``min`` and ``max`` constraints.
ORDERED_TYPES = frozenset(
    {
        AttributeType.INTEGER,
        AttributeType.DECIMAL,
        AttributeType.FLOAT,
        AttributeType.DATE,
        AttributeType.TIMESTAMP,
        AttributeType.DURATION,
    }
)

#: Types that cannot be array elements: arrays hold scalars only (AC-05).
NON_ELEMENT_TYPES = frozenset({AttributeType.ARRAY, AttributeType.JSON})


class GeoPoint(NamedTuple):
    """Decoded ``geo_point`` value."""

    lat: float
    lon: float


class TypeSpec(BaseModel):
    """A parameterized type: a base type plus the parameters that type requires.

    - ``decimal`` requires ``precision`` (total digits, at least 1) and ``scale``
      (fractional digits, ``0 <= scale <= precision``).
    - ``enum`` requires ``enum_values``: a closed, non-empty list of distinct strings.
    - ``array`` requires ``element``: a scalar type spec (no arrays of arrays, no json).
    - ``ref`` requires ``kind_ref``: the id of the Kind whose Record keys it holds.

    Other parameters must be absent. Constraints (``min``, ``max``, ``pattern`` and so on)
    belong to the Attribute, not to the type (see ``Constraints``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: AttributeType
    precision: int | None = Field(default=None, ge=1)
    scale: int | None = Field(default=None, ge=0)
    enum_values: tuple[str, ...] | None = None
    element: TypeSpec | None = None
    kind_ref: str | None = Field(default=None, pattern=KIND_ID_PATTERN)

    @field_validator("enum_values")
    @classmethod
    def _normalize_enum_values(cls, values: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if values is None:
            return None
        normalized = tuple(unicodedata.normalize("NFC", v) for v in values)
        if not normalized:
            raise ValueError("enum_values must not be empty")
        if any(v == "" for v in normalized):
            raise ValueError("enum values must be non-empty strings")
        if len(set(normalized)) != len(normalized):
            raise ValueError("enum values must be distinct")
        return normalized

    @model_validator(mode="after")
    def _check_parameters(self) -> Self:
        name = self.name
        if name is AttributeType.DECIMAL:
            if self.precision is None or self.scale is None:
                raise ValueError("decimal requires precision and scale")
            if self.scale > self.precision:
                raise ValueError("decimal scale must not exceed precision")
        elif self.precision is not None or self.scale is not None:
            raise ValueError(f"{name} does not take precision or scale")

        if name is AttributeType.ENUM:
            if self.enum_values is None:
                raise ValueError("enum requires a non-empty enum_values list")
        elif self.enum_values is not None:
            raise ValueError(f"{name} does not take enum_values")

        if name is AttributeType.ARRAY:
            if self.element is None:
                raise ValueError("array requires an element type")
            if self.element.name in NON_ELEMENT_TYPES:
                raise ValueError(f"array elements must be scalar, not {self.element.name}")
        elif self.element is not None:
            raise ValueError(f"{name} does not take an element type")

        if name is AttributeType.REF:
            if self.kind_ref is None:
                raise ValueError("ref requires kind_ref")
        elif self.kind_ref is not None:
            raise ValueError(f"{name} does not take kind_ref")
        return self

    # Convenience constructors -------------------------------------------------

    @classmethod
    def of(cls, name: AttributeType | str) -> TypeSpec:
        """Spec of a base type without parameters."""
        return cls(name=AttributeType(name))

    @classmethod
    def decimal(cls, precision: int, scale: int) -> TypeSpec:
        return cls(name=AttributeType.DECIMAL, precision=precision, scale=scale)

    @classmethod
    def enum(cls, *values: str) -> TypeSpec:
        return cls(name=AttributeType.ENUM, enum_values=tuple(values))

    @classmethod
    def array(cls, element: TypeSpec) -> TypeSpec:
        return cls(name=AttributeType.ARRAY, element=element)

    @classmethod
    def ref(cls, kind_id: str) -> TypeSpec:
        return cls(name=AttributeType.REF, kind_ref=kind_id)

    # Introspection ------------------------------------------------------------

    @property
    def is_array(self) -> bool:
        return self.name is AttributeType.ARRAY

    @property
    def scalar(self) -> TypeSpec:
        """The element spec for arrays, the spec itself otherwise."""
        if self.element is not None:
            return self.element
        return self

    def describe(self) -> str:
        """Human-readable form, for example ``decimal(12,2)`` or ``array<string>``."""
        if self.name is AttributeType.DECIMAL:
            return f"decimal({self.precision},{self.scale})"
        if self.name is AttributeType.ARRAY and self.element is not None:
            return f"array<{self.element.describe()}>"
        if self.name is AttributeType.REF:
            return f"ref({self.kind_ref})"
        return str(self.name)
