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
    """A typed field. ``default`` is a value of ``type`` (stored in canonical form).

    ``vault`` stores the values of a ``pii`` Attribute of a Kind in the vault (design 13.5):
    Record objects hold a vault reference instead of the value (``lorebase_core.model.vault``).
    Unset (None) keeps values inline; the field is then left out of canonical content, so
    schemas written before it keep their hashes.

    ``valid_time`` lets values carry a validity period on their provenance entry (design 5.4,
    10.6): reconciliation then tells superseded values from contradictions. Like ``vault`` it
    is absent from content when unset.
    """

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
    vault: bool | None = None
    valid_time: bool | None = None

    @field_validator("sensitivity")
    @classmethod
    def _sort_sensitivity(cls, tags: tuple[Sensitivity, ...]) -> tuple[Sensitivity, ...]:
        return tuple(sorted(set(tags)))

    @property
    def is_derived(self) -> bool:
        return self.derivation_id is not None

    @property
    def is_sensitive(self) -> bool:
        """Whether reads need a clearance for this Attribute's values (design 10.2)."""
        return bool(self.sensitivity)

    @property
    def in_vault(self) -> bool:
        """Whether values are stored in the vault, Record objects holding references."""
        return self.vault is True

    @property
    def has_valid_time(self) -> bool:
        """Whether values carry validity periods on their provenance (design 10.6)."""
        return self.valid_time is True
