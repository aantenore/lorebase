"""Derivation (design 5.8): a computed Attribute or Relation. Model only; no evaluation here.

Derived values are not part of Record content and never merge (AC-07).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar, Literal, Self

from pydantic import Field, model_validator

from lorebase_core.model.base import ElementStatus, Model, SchemaElement
from lorebase_core.model.ids import AttributeId, DerivationId, KindId, RelationId
from lorebase_core.model.operation import Expression
from lorebase_core.model.relation import LinkDirection


class AggregateFunction(StrEnum):
    COUNT = "count"
    SUM = "sum"
    MIN = "min"
    MAX = "max"
    AVG = "avg"


class ExpressionDerivation(Model):
    """A CEL expression over the Record's own Attributes, computing ``kind.attribute``."""

    variant: Literal["expression"] = "expression"
    kind: KindId
    attribute: AttributeId
    expression: Expression


class AggregateDerivation(Model):
    """An aggregate over the Records reached from ``kind`` through ``relation`` in ``direction``.

    ``source_attribute`` is an Attribute of the Kind at the far end; ``count`` needs none.
    """

    variant: Literal["aggregate"] = "aggregate"
    kind: KindId
    attribute: AttributeId
    relation: RelationId
    direction: LinkDirection = LinkDirection.FORWARD
    function: AggregateFunction
    source_attribute: AttributeId | None = None
    filter: Expression | None = None

    @model_validator(mode="after")
    def _check_source(self) -> Self:
        if self.function is not AggregateFunction.COUNT and self.source_attribute is None:
            raise ValueError(f"aggregate {self.function} requires source_attribute")
        return self


class PathStep(Model):
    relation: RelationId
    direction: LinkDirection = LinkDirection.FORWARD


class PathDerivation(Model):
    """A composed Relation ``A -r1-> B -r2-> C`` exposed as a virtual Relation ``A -> C``.

    The virtual Relation's name is the Derivation's ``api_name``.
    """

    variant: Literal["path"] = "path"
    steps: tuple[PathStep, ...] = Field(min_length=2)
    inverse_api_name: str | None = None
    materialized: bool = False


DerivationDefinition = Annotated[
    ExpressionDerivation | AggregateDerivation | PathDerivation,
    Field(discriminator="variant"),
]


class Derivation(SchemaElement):
    object_type: ClassVar[str] = "schema.derivation"

    id: DerivationId
    definition: DerivationDefinition
    status: ElementStatus = ElementStatus.ACTIVE

    @property
    def target(self) -> tuple[KindId, AttributeId] | None:
        """The ``(kind, attribute)`` this Derivation computes; ``None`` for path Derivations."""
        definition = self.definition
        if isinstance(definition, ExpressionDerivation | AggregateDerivation):
            return (definition.kind, definition.attribute)
        return None
