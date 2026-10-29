"""Lineage (design 5.11, AC-08): provenance entries attached to changes, not to content.

A provenance entry describes one origin. Each change in a Revision changeset carries a
``LineageMap`` from the Attribute ids it changed to provenance entry ids. Provenance is never
part of content hashes.

A value of a ``valid_time`` Attribute may carry a validity period on its entry (``valid_from``,
``valid_to``, design 10.6). A ``reconciliation`` origin marks a value written by a decision of
the reconciliation queue; only Keeper builds one, callers never name an origin.
"""

from __future__ import annotations

from datetime import datetime
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
    RECONCILIATION = "reconciliation"


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


#: Prefixes of a reconciliation resolver: the person who decided, or the policy applied.
RESOLVER_PREFIXES = ("principal:", "policy:")


class ReconciliationOrigin(Model):
    """A value written by a decision on reconciliation candidate ``candidate_id`` (design 10.6).

    ``claim_id`` is the chosen claim (unset for a custom value); ``resolver`` is
    ``principal:{id}`` or ``policy:{name}``; ``source`` is the source identity of the chosen
    claim, so that the value keeps the identity it came from.
    """

    type: Literal["reconciliation"] = "reconciliation"
    candidate_id: str
    claim_id: str | None = None
    resolver: str
    source: str | None = None

    @field_validator("resolver")
    @classmethod
    def _resolver(cls, value: str) -> str:
        prefix = next((p for p in RESOLVER_PREFIXES if value.startswith(p)), None)
        if prefix is None or len(value) == len(prefix):
            raise ValueError("resolver must be principal:{id} or policy:{name}")
        return value


Origin = Annotated[
    OperationOrigin
    | SourceSyncOrigin
    | ExtractionOrigin
    | MergeResolutionOrigin
    | SchemaMigrationOrigin
    | ImportOrigin
    | ReconciliationOrigin,
    Field(discriminator="type"),
]


class ProvenanceEntry(Model):
    """One Lineage origin: ``{id, origin, actor, at}``; ``at`` is a canonical timestamp.

    ``valid_from`` and ``valid_to`` (canonical timestamps, either may be open) are the
    validity period of the values of a ``valid_time`` Attribute the entry wrote; unset, the
    entry has no period and is left out of content as before.
    """

    id: ProvenanceId
    origin: Origin
    actor: str
    at: str
    valid_from: str | None = None
    valid_to: str | None = None

    @field_validator("at", mode="before")
    @classmethod
    def _canonical_timestamp(cls, value: object) -> object:
        return encode_value(_TIMESTAMP, value)

    @field_validator("valid_from", "valid_to", mode="before")
    @classmethod
    def _canonical_period(cls, value: object) -> object:
        return None if value is None else encode_value(_TIMESTAMP, value)

    @model_validator(mode="after")
    def _check_period(self) -> Self:
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and datetime.fromisoformat(self.valid_to) < datetime.fromisoformat(self.valid_from)
        ):
            raise ValueError("valid_to must not precede valid_from")
        return self

    @property
    def origin_type(self) -> OriginType:
        return OriginType(self.origin.type)


#: Per change: changed Attribute id -> provenance entry id.
LineageMap = dict[AttributeId, ProvenanceId]
