"""Operation (design 5.7): a named, typed, governed write.

Expressions (preconditions, step values, review policies) are CEL source strings (AC-06).
They are stored as written; type checking and evaluation belong to the expression module.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar, Literal, Self

from pydantic import Field, model_validator

from lorebase_core.model.base import ElementStatus, Model, SchemaElement, snake_case
from lorebase_core.model.ids import AttributeId, KindId, OperationId, RelationId
from lorebase_core.model.kind import BuiltinOperation, Kind
from lorebase_core.model.relation import Relation
from lorebase_core.types import TypeSpec

#: A CEL expression, stored as source text.
Expression = Annotated[str, Field(min_length=1)]


class OperationParameter(Model):
    """A parameter. Record reference parameters use a ``ref`` type naming the Kind."""

    name: str
    type: TypeSpec
    required: bool = False
    description: str = ""


class Precondition(Model):
    expression: Expression
    message: str


class CreateRecordStep(Model):
    step: Literal["create_record"] = "create_record"
    kind: KindId
    values: dict[AttributeId, Expression] = Field(default_factory=dict)


class UpdateRecordStep(Model):
    step: Literal["update_record"] = "update_record"
    kind: KindId
    target: Expression
    values: dict[AttributeId, Expression] = Field(default_factory=dict)


class DeleteRecordStep(Model):
    step: Literal["delete_record"] = "delete_record"
    kind: KindId
    target: Expression


class CreateLinkStep(Model):
    step: Literal["create_link"] = "create_link"
    relation: RelationId
    from_: Expression = Field(alias="from")
    to: Expression
    values: dict[AttributeId, Expression] = Field(default_factory=dict)


class DeleteLinkStep(Model):
    step: Literal["delete_link"] = "delete_link"
    relation: RelationId
    from_: Expression = Field(alias="from")
    to: Expression


OperationStep = Annotated[
    CreateRecordStep | UpdateRecordStep | DeleteRecordStep | CreateLinkStep | DeleteLinkStep,
    Field(discriminator="step"),
]


class ReviewMode(StrEnum):
    NEVER = "never"
    ALWAYS = "always"
    EXPRESSION = "expression"


class ReviewPolicy(Model):
    """``never``, ``always``, or a CEL ``expression`` (true sends the edits to a Proposal)."""

    mode: ReviewMode = ReviewMode.NEVER
    expression: Expression | None = None

    @model_validator(mode="after")
    def _check_expression(self) -> Self:
        if (self.mode is ReviewMode.EXPRESSION) != (self.expression is not None):
            raise ValueError(
                "review policy has an expression if and only if its mode is 'expression'"
            )
        return self


class RateLimit(Model):
    max_calls: int = Field(ge=1)
    per_seconds: int = Field(ge=1)


class Operation(SchemaElement):
    object_type: ClassVar[str] = "schema.operation"

    id: OperationId
    parameters: tuple[OperationParameter, ...] = ()
    preconditions: tuple[Precondition, ...] = ()
    steps: tuple[OperationStep, ...] = ()
    review_policy: ReviewPolicy = Field(default_factory=ReviewPolicy)
    permissions: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ()
    rate_limit: RateLimit | None = None
    status: ElementStatus = ElementStatus.ACTIVE

    @property
    def uses_expressions(self) -> bool:
        """True when the Operation carries CEL expressions."""
        return (
            bool(self.preconditions or self.steps)
            or self.review_policy.mode is ReviewMode.EXPRESSION
        )


def builtin_operation_names(kind: Kind) -> list[str]:
    """Names of the built-in Operations enabled for ``kind``: ``create_{kind}`` and so on."""
    base = snake_case(kind.api_name)
    return [f"{operation.value}_{base}" for operation in kind.operations_builtin]


def builtin_link_operation_names(relation: Relation) -> list[str]:
    """``link_{relation}`` and ``unlink_{relation}`` for an explicit Relation."""
    return [f"link_{relation.api_name}", f"unlink_{relation.api_name}"]


__all__ = [
    "BuiltinOperation",
    "CreateLinkStep",
    "CreateRecordStep",
    "DeleteLinkStep",
    "DeleteRecordStep",
    "Expression",
    "Operation",
    "OperationParameter",
    "OperationStep",
    "Precondition",
    "RateLimit",
    "ReviewMode",
    "ReviewPolicy",
    "UpdateRecordStep",
    "builtin_link_operation_names",
    "builtin_operation_names",
]
