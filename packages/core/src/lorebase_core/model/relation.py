"""Relation (design 5.5): a typed, directional connection between two Kinds."""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar, Self

from pydantic import Field, model_validator

from lorebase_core.model.attribute import Attribute
from lorebase_core.model.base import ElementStatus, SchemaElement
from lorebase_core.model.ids import AttributeId, KindId, RelationId
from lorebase_core.model.kind import check_attribute_keys


class Cardinality(StrEnum):
    """``x_to_y``: how many ``from`` Records (x) and ``to`` Records (y) one Link side allows.

    ``one_to_many``: each ``to`` Record has at most one incoming Link;
    ``many_to_one``: each ``from`` Record has at most one outgoing Link.
    """

    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"


class OnDelete(StrEnum):
    CASCADE = "cascade"
    RESTRICT = "restrict"


class RelationBacking(StrEnum):
    """``explicit``: Links are stored. ``foreign_key``: Links derive from a ``ref`` Attribute."""

    EXPLICIT = "explicit"
    FOREIGN_KEY = "foreign_key"


class LinkDirection(StrEnum):
    FORWARD = "forward"
    INVERSE = "inverse"


class Relation(SchemaElement):
    object_type: ClassVar[str] = "schema.relation"

    id: RelationId
    inverse_api_name: str
    from_kind: KindId
    to_kind: KindId
    cardinality: Cardinality = Cardinality.MANY_TO_MANY
    required_from: bool = False
    attributes: dict[AttributeId, Attribute] = Field(default_factory=dict)
    on_delete: OnDelete = OnDelete.CASCADE
    backing: RelationBacking = RelationBacking.EXPLICIT
    #: For ``foreign_key`` backing: the ``ref`` Attribute of ``from_kind`` holding the ``to`` key.
    foreign_key_attribute: AttributeId | None = None
    status: ElementStatus = ElementStatus.ACTIVE

    @model_validator(mode="after")
    def _check_keys(self) -> Self:
        check_attribute_keys(self.attributes, f"Relation {self.id}")
        return self

    @property
    def single_per_from(self) -> bool:
        """Each ``from`` Record has at most one Link of this Relation."""
        return self.cardinality in (Cardinality.ONE_TO_ONE, Cardinality.MANY_TO_ONE)

    @property
    def single_per_to(self) -> bool:
        """Each ``to`` Record has at most one Link of this Relation."""
        return self.cardinality in (Cardinality.ONE_TO_ONE, Cardinality.ONE_TO_MANY)

    def endpoint(self, direction: LinkDirection) -> KindId:
        """The Kind a traversal in ``direction`` starts from."""
        return self.from_kind if direction is LinkDirection.FORWARD else self.to_kind

    def far_endpoint(self, direction: LinkDirection) -> KindId:
        """The Kind a traversal in ``direction`` arrives at."""
        return self.to_kind if direction is LinkDirection.FORWARD else self.from_kind
