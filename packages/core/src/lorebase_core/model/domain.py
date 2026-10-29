"""Domain (design 5.2): the isolation boundary for data, permissions and storage."""

from __future__ import annotations

from typing import Annotated, ClassVar

from pydantic import Field, JsonValue, StringConstraints, field_validator

from lorebase_core.model.base import (
    MAX_STEWARD_ROLE_CHARS,
    MAX_STEWARD_ROLES,
    Model,
    steward_roles,
)
from lorebase_core.types import AttributeType, TypeSpec, encode_value

DomainSlug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]*$")]
#: A role name in a steward list (design 10.7).
StewardRole = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_STEWARD_ROLE_CHARS)
]

_TIMESTAMP = TypeSpec.of(AttributeType.TIMESTAMP)

#: Upper bounds of the example questions of a Domain; they are shown as buttons.
MAX_EXAMPLES = 20
MAX_EXAMPLE_LABEL = 80
MAX_EXAMPLE_QUESTION = 500


class DomainExample(Model):
    """A sample question about the Domain, offered by clients as a ready-made prompt."""

    label: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_EXAMPLE_LABEL)
    ]
    question: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_EXAMPLE_QUESTION),
    ]


class Domain(Model):
    object_type: ClassVar[str] = "schema.domain"

    id: DomainSlug
    display_name: str = ""
    description: str = ""
    default_branch: str = "main"
    settings: dict[str, JsonValue] = Field(default_factory=dict)
    #: Optional sample questions; absent (not empty) when there are none, so the canonical
    #: content of Domains written before this field existed is unchanged.
    examples: tuple[DomainExample, ...] | None = Field(default=None, max_length=MAX_EXAMPLES)
    #: Roles that review this Domain's Proposals and reconciliation candidates by default
    #: (design 10.7); absent when there are none, like `examples`.
    stewards: tuple[StewardRole, ...] | None = Field(default=None, max_length=MAX_STEWARD_ROLES)
    #: Canonical RFC 3339 UTC timestamp.
    created_at: str | None = None
    created_by: str | None = None

    @field_validator("created_at", mode="before")
    @classmethod
    def _canonical_timestamp(cls, value: object) -> object:
        return encode_value(_TIMESTAMP, value)

    @field_validator("examples", mode="before")
    @classmethod
    def _empty_examples_are_absent(cls, value: object) -> object:
        if isinstance(value, list | tuple) and len(value) == 0:
            return None
        return value

    @field_validator("stewards", mode="before")
    @classmethod
    def _empty_stewards_are_absent(cls, value: object) -> object:
        return steward_roles(value)

    @field_validator("examples")
    @classmethod
    def _unique_labels(
        cls, value: tuple[DomainExample, ...] | None
    ) -> tuple[DomainExample, ...] | None:
        if value is not None:
            labels = [example.label for example in value]
            duplicates = sorted({label for label in labels if labels.count(label) > 1})
            if duplicates:
                raise ValueError(f"duplicate example labels: {', '.join(duplicates)}")
        return value
