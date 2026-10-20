"""Domain (design 5.2): the isolation boundary for data, permissions and storage."""

from __future__ import annotations

from typing import Annotated, ClassVar

from pydantic import Field, JsonValue, StringConstraints, field_validator

from lorebase_core.model.base import Model
from lorebase_core.types import AttributeType, TypeSpec, encode_value

DomainSlug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]*$")]

_TIMESTAMP = TypeSpec.of(AttributeType.TIMESTAMP)


class Domain(Model):
    object_type: ClassVar[str] = "schema.domain"

    id: DomainSlug
    display_name: str = ""
    description: str = ""
    default_branch: str = "main"
    settings: dict[str, JsonValue] = Field(default_factory=dict)
    #: Canonical RFC 3339 UTC timestamp.
    created_at: str | None = None
    created_by: str | None = None

    @field_validator("created_at", mode="before")
    @classmethod
    def _canonical_timestamp(cls, value: object) -> object:
        return encode_value(_TIMESTAMP, value)
