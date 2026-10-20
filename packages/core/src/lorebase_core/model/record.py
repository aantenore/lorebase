"""Record and Link (design 5.1, 5.10).

A Record is identified by ``(kind, key)`` (AC-02); a Link by ``(relation, from_key, to_key)``,
with no parallel Links (AC-03). Values are keyed by Attribute id and hold canonical values;
null and absent are the same, so ``None`` values are dropped.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar, NamedTuple
from urllib.parse import quote

from pydantic import Field, JsonValue, field_validator

from lorebase_core.model.base import Model
from lorebase_core.model.ids import AttributeId, KindId, RelationId
from lorebase_core.model.kind import Kind
from lorebase_core.types import normalize_key, to_text

#: Separator between Kind api name and key in external references (``Supplier/S-1001``).
REF_SEPARATOR = "/"
#: Separator between components of composite keys (components are percent-encoded).
KEY_COMPONENT_SEPARATOR = ","


class RecordIdentity(NamedTuple):
    kind: str
    key: str


class LinkIdentity(NamedTuple):
    relation: str
    from_key: str
    to_key: str


def _drop_absent(values: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {attribute: value for attribute, value in values.items() if value is not None}


class Record(Model):
    object_type: ClassVar[str] = "record"

    kind: KindId
    key: str
    values: dict[AttributeId, JsonValue] = Field(default_factory=dict)

    @field_validator("key")
    @classmethod
    def _normalize_key(cls, key: str) -> str:
        return normalize_key(key)

    @field_validator("values")
    @classmethod
    def _drop_absent_values(cls, values: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _drop_absent(values)

    @property
    def identity(self) -> RecordIdentity:
        return RecordIdentity(self.kind, self.key)


class Link(Model):
    object_type: ClassVar[str] = "link"

    relation: RelationId
    from_key: str = Field(alias="from")
    to_key: str = Field(alias="to")
    values: dict[AttributeId, JsonValue] = Field(default_factory=dict)

    @field_validator("from_key", "to_key")
    @classmethod
    def _normalize_key(cls, key: str) -> str:
        return normalize_key(key)

    @field_validator("values")
    @classmethod
    def _drop_absent_values(cls, values: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _drop_absent(values)

    @property
    def identity(self) -> LinkIdentity:
        return LinkIdentity(self.relation, self.from_key, self.to_key)


def render_key(components: Sequence[str]) -> str:
    """Render primary key component texts as a canonical key string.

    A single component is the key itself; composite keys percent-encode each component and
    join them with ``,`` so that rendering is unambiguous.
    """
    if not components:
        raise ValueError("a key needs at least one component")
    if len(components) == 1:
        return components[0]
    return KEY_COMPONENT_SEPARATOR.join(quote(component, safe="") for component in components)


def record_key(kind: Kind, values: Mapping[str, JsonValue]) -> str | None:
    """The key of a Record of ``kind`` from its canonical primary key values.

    Returns ``None`` when the Kind has no natural key or a primary key value is absent.
    """
    if not kind.primary_key:
        return None
    components: list[str] = []
    for attribute_id in kind.primary_key:
        value = values.get(attribute_id)
        attribute = kind.attributes.get(attribute_id)
        if value is None or attribute is None:
            return None
        components.append(to_text(attribute.type, value))
    return render_key(components)


def format_ref(kind_api_name: str, key: str) -> str:
    """External reference ``{kind_api_name}/{key}`` (design 5.1)."""
    return f"{kind_api_name}{REF_SEPARATOR}{key}"


def parse_ref(ref: str) -> tuple[str, str]:
    """Split an external reference into ``(kind_api_name, key)``; keys may contain ``/``."""
    kind_api_name, separator, key = ref.partition(REF_SEPARATOR)
    if not separator or not kind_api_name or not key:
        raise ValueError(f"not a Record reference: {ref!r}")
    return kind_api_name, key
