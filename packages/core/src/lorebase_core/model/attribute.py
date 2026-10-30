"""Attribute (design 5.4): a typed field on a Kind, or a link Attribute on a Relation."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, JsonValue, field_validator

from lorebase_core.model.base import Model
from lorebase_core.model.ids import AttributeId, DerivationId, SourceId
from lorebase_core.types import AttributeType, Constraints, TypeSpec

#: Types whose values (or array elements) may be held by the value index (design 8.9).
VALUE_RESOLVABLE_TYPES = frozenset({AttributeType.STRING, AttributeType.ENUM})


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


#: Aliases of one element (a Kind, an Attribute, a value), at most this many and this long.
MAX_ALIASES = 50
MAX_ALIAS_CHARS = 128
#: Values of one Attribute that carry aliases, at most this many.
MAX_ALIASED_VALUES = 1000


def alias_list(value: object) -> object:
    """Normalize an alias list before validation: surrounding blanks and empty entries are
    dropped, duplicates removed in order, and an empty list is the same as none (absent, so
    canonical content without aliases is unchanged)."""
    if isinstance(value, list | tuple):
        cleaned = [a.strip() if isinstance(a, str) else a for a in value]
        kept = tuple(dict.fromkeys(a for a in cleaned if a != ""))
        return kept or None
    return value


class Attribute(Model):
    """A typed field. ``default`` is a value of ``type`` (stored in canonical form).

    ``vault`` stores the values of a ``pii`` Attribute of a Kind in the vault (design 13.5):
    Record objects hold a vault reference instead of the value (``lorebase_core.model.vault``).
    Unset (None) keeps values inline; the field is then left out of canonical content, so
    schemas written before it keep their hashes.

    ``valid_time`` lets values carry a validity period on their provenance entry (design 5.4,
    10.6): reconciliation then tells superseded values from contradictions. Like ``vault`` it
    is absent from content when unset.

    ``aliases`` (other words for the Attribute), ``value_aliases`` (other words for some of its
    values, keyed by the stored value) and ``resolvable`` (force the Attribute into the value
    index, or out of it; unset: decided by type and distinct values) serve the resolution of
    words to schema and values (design 8.9). All three are absent from content when unset.
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
    aliases: tuple[str, ...] | None = None
    value_aliases: dict[str, tuple[str, ...]] | None = Field(
        default=None, max_length=MAX_ALIASED_VALUES
    )
    resolvable: bool | None = None

    @field_validator("aliases", mode="before")
    @classmethod
    def _clean_aliases(cls, value: object) -> object:
        return alias_list(value)

    @field_validator("value_aliases", mode="before")
    @classmethod
    def _clean_value_aliases(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        cleaned = {k: alias_list(v) for k, v in value.items()}
        kept = {k: v for k, v in cleaned.items() if v is not None}
        return kept or None

    @field_validator("aliases")
    @classmethod
    def _bound_aliases(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return bounded_aliases(value)

    @field_validator("value_aliases")
    @classmethod
    def _bound_value_aliases(
        cls, value: dict[str, tuple[str, ...]] | None
    ) -> dict[str, tuple[str, ...]] | None:
        if value is not None:
            for aliases in value.values():
                bounded_aliases(aliases)
        return value

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
    def value_resolvable(self) -> bool:
        """Whether the type allows a value index (design 8.9): short text with a closed or
        small set of values, alone or in an array; never long text, keys or numbers."""
        spec = self.type.element if self.type.name is AttributeType.ARRAY else self.type
        return spec is not None and spec.name in VALUE_RESOLVABLE_TYPES

    @property
    def has_valid_time(self) -> bool:
        """Whether values carry validity periods on their provenance (design 10.6)."""
        return self.valid_time is True


def bounded_aliases(aliases: tuple[str, ...] | None) -> tuple[str, ...] | None:
    """`aliases`, refused when there are too many or one is too long."""
    if aliases is None:
        return None
    if len(aliases) > MAX_ALIASES:
        raise ValueError(f"at most {MAX_ALIASES} aliases")
    for alias in aliases:
        if len(alias) > MAX_ALIAS_CHARS:
            raise ValueError(f"an alias is longer than {MAX_ALIAS_CHARS} characters")
    return aliases
