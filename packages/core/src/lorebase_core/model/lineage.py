"""Lineage (design 5.11, AC-08): provenance entries attached to changes, not to content.

A provenance entry describes one origin. Each change in a Revision changeset carries a
``LineageMap`` from the Attribute ids it changed to provenance entry ids. Provenance is never
part of content hashes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from lorebase_core.model.base import Model
from lorebase_core.model.ids import AttributeId, OperationId, ProvenanceId, SourceId
from lorebase_core.types import AttributeType, TypeSpec, encode_value

_TIMESTAMP = TypeSpec.of(AttributeType.TIMESTAMP)


class OriginType(StrEnum):
    OPERATION = "operation"
    SOURCE_SYNC = "source_sync"
    EXTRACTION = "extraction"
    MERGE_RESOLUTION = "merge_resolution"
    SCHEMA_MIGRATION = "schema_migration"
    IMPORT = "import"


class OperationOrigin(Model):
    type: Literal["operation"] = "operation"
    invocation_id: str
    #: Operation api name; built-in Operations (``create_supplier``) have no stored id.
    operation: str
    operation_id: OperationId | None = None
    principal: str


class SourceSyncOrigin(Model):
    type: Literal["source_sync"] = "source_sync"
    source_id: SourceId
    run_id: str
    row_locator: str | None = None


class ExtractionOrigin(Model):
    type: Literal["extraction"] = "extraction"
    job_id: str
    document_id: str
    span_start: int = Field(ge=0)
    span_end: int = Field(ge=0)
    model_id: str
    prompt_version: str

    @model_validator(mode="after")
    def _check_span(self) -> Self:
        if self.span_end < self.span_start:
            raise ValueError("span_end must not precede span_start")
        return self


class MergeResolutionOrigin(Model):
    type: Literal["merge_resolution"] = "merge_resolution"
    proposal_id: str
    conflict_id: str
    resolver: str


class SchemaMigrationOrigin(Model):
    """A value written by a schema change (cast or default) on ``attribute_id``."""

    type: Literal["schema_migration"] = "schema_migration"
    attribute_id: AttributeId
    from_type: TypeSpec | None = None
    to_type: TypeSpec | None = None


class ImportOrigin(Model):
    type: Literal["import"] = "import"
    batch_id: str


Origin = Annotated[
    OperationOrigin
    | SourceSyncOrigin
    | ExtractionOrigin
    | MergeResolutionOrigin
    | SchemaMigrationOrigin
    | ImportOrigin,
    Field(discriminator="type"),
]


class ProvenanceEntry(Model):
    """One Lineage origin: ``{id, origin, actor, at}``; ``at`` is a canonical timestamp."""

    id: ProvenanceId
    origin: Origin
    actor: str
    at: str

    @field_validator("at", mode="before")
    @classmethod
    def _canonical_timestamp(cls, value: object) -> object:
        return encode_value(_TIMESTAMP, value)

    @property
    def origin_type(self) -> OriginType:
        return OriginType(self.origin.type)


#: Per change: changed Attribute id -> provenance entry id.
LineageMap = dict[AttributeId, ProvenanceId]
