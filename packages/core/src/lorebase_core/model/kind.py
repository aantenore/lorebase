"""Kind (design 5.3): a type of thing in a Domain."""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar, Self

from pydantic import Field, field_validator, model_validator

from lorebase_core.model.attribute import Attribute, alias_list, bounded_aliases
from lorebase_core.model.base import (
    MAX_STEWARD_ROLES,
    ElementStatus,
    Model,
    SchemaElement,
    steward_roles,
)
from lorebase_core.model.domain import StewardRole
from lorebase_core.model.ids import AttributeId, KindId, TraitId


class BuiltinOperation(StrEnum):
    """Built-in Operations a Kind can enable (design 5.7)."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class TraitImplementation(Model):
    """A Kind implementing a Trait.

    ``attribute_map`` maps Trait Attribute ids to Kind Attribute ids.
    """

    trait_id: TraitId
    attribute_map: dict[AttributeId, AttributeId] = Field(default_factory=dict)


class KindSearch(Model):
    """Search settings of a Kind. Full-text fields come from searchable Attributes.

    ``embedding_template`` opts the Kind into semantic search: Scribe embeds the text it
    renders for each Record (``lorebase_core.model.embedding``). ``embedding_sensitive`` lets
    the template name sensitive Attributes; unset, they are refused.
    """

    embedding_template: str | None = None
    embedding_sensitive: bool | None = None


def check_attribute_keys(attributes: dict[str, Attribute], owner: str) -> None:
    for key, attribute in attributes.items():
        if key != attribute.id:
            raise ValueError(
                f"{owner}: attribute map key {key} does not match attribute id {attribute.id}"
            )


class Kind(SchemaElement):
    object_type: ClassVar[str] = "schema.kind"

    id: KindId
    primary_key: tuple[AttributeId, ...] = ()
    title_attribute: AttributeId | None = None
    attributes: dict[AttributeId, Attribute] = Field(default_factory=dict)
    traits: tuple[TraitImplementation, ...] = ()
    status: ElementStatus = ElementStatus.ACTIVE
    search: KindSearch = Field(default_factory=KindSearch)
    icon: str | None = None
    color: str | None = None
    operations_builtin: tuple[BuiltinOperation, ...] = (
        BuiltinOperation.CREATE,
        BuiltinOperation.UPDATE,
        BuiltinOperation.DELETE,
    )
    #: Steward roles of work touching this Kind; they replace the Domain's (design 10.7).
    #: Absent when none.
    stewards: tuple[StewardRole, ...] | None = Field(default=None, max_length=MAX_STEWARD_ROLES)
    #: Other words for the Kind, for the resolution of words to schema (design 8.9). Absent
    #: when none.
    aliases: tuple[str, ...] | None = None

    @field_validator("aliases", mode="before")
    @classmethod
    def _clean_aliases(cls, value: object) -> object:
        return alias_list(value)

    @field_validator("aliases")
    @classmethod
    def _bound_aliases(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return bounded_aliases(value)

    @field_validator("stewards", mode="before")
    @classmethod
    def _empty_stewards_are_absent(cls, value: object) -> object:
        return steward_roles(value)

    @model_validator(mode="after")
    def _check_keys(self) -> Self:
        check_attribute_keys(self.attributes, f"Kind {self.id}")
        return self

    @property
    def has_natural_key(self) -> bool:
        """True when Records are keyed by primary key values, False when keys are minted ULIDs."""
        return bool(self.primary_key)

    def attribute_by_api_name(self, api_name: str) -> Attribute | None:
        for attribute in self.attributes.values():
            if attribute.api_name == api_name:
                return attribute
        return None
