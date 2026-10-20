"""Attribute (design 5.4): a typed field on a Kind, or a link Attribute on a Relation."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, JsonValue, field_validator

from lorebase_core.model.base import Model
from lorebase_core.model.ids import AttributeId, DerivationId, SourceId
from lorebase_core.types import Constraints, TypeSpec


class Sensitivity(StrEnum):
    PII = "pii"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class SearchWeight(StrEnum):
    """Full-text weight of a searchable Attribute (``A`` is the strongest)."""

    A = "A"
    B = "B"
    C = "C"
    D = "D"


class Attribute(Model):
    """A typed field. ``default`` is a value of ``type`` (stored in canonical form)."""

    id: AttributeId
    api_name: str
    display_name: str = ""
    description: str = ""
    type: TypeSpec
    required: bool = False
    unique: bool = False
    default: JsonValue = None
    constraints: Constraints = Field(default_factory=Constraints)
    sensitivity: tuple[Sensitivity, ...] = ()
    indexed: bool = False
    searchable: bool = False
    search_weight: SearchWeight | None = None
    derivation_id: DerivationId | None = None
    authority: SourceId | None = None

    @field_validator("sensitivity")
    @classmethod
    def _sort_sensitivity(cls, tags: tuple[Sensitivity, ...]) -> tuple[Sensitivity, ...]:
        return tuple(sorted(set(tags)))

    @property
    def is_derived(self) -> bool:
        return self.derivation_id is not None
