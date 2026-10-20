"""Trait (design 5.6): a shared set of Attributes that Kinds implement by mapping."""

from __future__ import annotations

from typing import ClassVar, Self

from pydantic import Field, model_validator

from lorebase_core.model.base import ElementStatus, Model, SchemaElement
from lorebase_core.model.ids import AttributeId, TraitId
from lorebase_core.types import TypeSpec


class TraitAttribute(Model):
    id: AttributeId
    api_name: str
    display_name: str = ""
    description: str = ""
    type: TypeSpec
    required: bool = False


class Trait(SchemaElement):
    object_type: ClassVar[str] = "schema.trait"

    id: TraitId
    attributes: dict[AttributeId, TraitAttribute] = Field(default_factory=dict)
    status: ElementStatus = ElementStatus.ACTIVE

    @model_validator(mode="after")
    def _check_keys(self) -> Self:
        for key, attribute in self.attributes.items():
            if key != attribute.id:
                raise ValueError(
                    f"Trait {self.id}: attribute map key {key} does not match id {attribute.id}"
                )
        return self
