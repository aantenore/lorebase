"""Claims and source identities (design 10.6).

A claim is one value for one ``(kind, key, attribute)`` and where it comes from: a provenance
entry when the value is or was on a branch, or a Proposal item with its evidence when it is
only proposed. Its source identity follows from the origin:

- ``source:{id}`` for Source syncs, ``document:{id}`` for extraction, ``principal:{id}`` for
  Operations, ``import:{batch}`` for bulk imports;
- ``merge_resolution`` and ``schema_migration`` origins keep the identity of the value they
  carried over, which the caller passes as ``chosen``; a ``reconciliation`` origin names it
  (``source``), or else the person who decided a custom value.

Value hashes are taken on the canonical value, so equal values from several sources hash alike.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from lorebase_core.canonical import canonical_bytes
from lorebase_core.model.lineage import (
    ExtractionOrigin,
    ImportOrigin,
    OperationOrigin,
    Origin,
    ProvenanceEntry,
    ReconciliationOrigin,
    SourceSyncOrigin,
)
from lorebase_core.types import AttributeType, TypeSpec, encode_value

_TIMESTAMP = TypeSpec.of(AttributeType.TIMESTAMP)


class IdentityKind(StrEnum):
    """Prefix of a source identity."""

    SOURCE = "source"
    DOCUMENT = "document"
    PRINCIPAL = "principal"
    IMPORT = "import"


def identity(kind: IdentityKind, ref: str) -> str:
    """``{kind}:{ref}``, for example ``source:src_01J...``."""
    return f"{kind.value}:{ref}"


def source_identity(origin: Origin, *, chosen: str | None = None) -> str | None:
    """The source identity of a value written by ``origin`` (module doc). ``chosen`` is the
    identity of the value a merge resolution or a schema migration carried over; ``None`` when
    it cannot be told."""
    if isinstance(origin, SourceSyncOrigin):
        return identity(IdentityKind.SOURCE, origin.source_id)
    if isinstance(origin, ExtractionOrigin):
        return identity(IdentityKind.DOCUMENT, origin.document_id)
    if isinstance(origin, OperationOrigin):
        return identity(IdentityKind.PRINCIPAL, origin.principal)
    if isinstance(origin, ImportOrigin):
        return identity(IdentityKind.IMPORT, origin.batch_id)
    if isinstance(origin, ReconciliationOrigin):
        if origin.source is not None:
            return origin.source
        if chosen is not None:
            return chosen
        prefix = f"{IdentityKind.PRINCIPAL.value}:"
        return origin.resolver if origin.resolver.startswith(prefix) else None
    return chosen


def value_hash(value: JsonValue) -> str:
    """Hex SHA-256 of the canonical JSON of a canonical value."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _timestamp(value: object) -> object:
    return None if value is None else encode_value(_TIMESTAMP, value)


class ProposalItemRef(BaseModel):
    """The Proposal item holding a claim that is only proposed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: str
    item_id: str


class Claim(BaseModel):
    """One value and where it comes from (module doc).

    ``claim_id`` is the provenance id, or ``{proposal_id}/{item_id}`` for a proposed value.
    ``value`` is canonical; ``value_hash`` is computed from it when left out. ``at`` is when
    the value was written or proposed; ``valid_from`` and ``valid_to`` its validity period, if
    any. Times are canonical timestamps.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str = Field(min_length=1)
    value: JsonValue
    value_hash: str = ""
    source: str | None = None
    provenance_id: str | None = None
    proposal_item: ProposalItemRef | None = None
    evidence: dict[str, JsonValue] | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    at: str

    @field_validator("at", "valid_from", "valid_to", mode="before")
    @classmethod
    def _canonical_time(cls, value: object) -> object:
        return _timestamp(value)

    @model_validator(mode="after")
    def _derived(self) -> Self:
        expected = value_hash(self.value)
        if not self.value_hash:
            object.__setattr__(self, "value_hash", expected)
        elif self.value_hash != expected:
            raise ValueError("value_hash does not match value")
        if (self.provenance_id is None) == (self.proposal_item is None):
            raise ValueError("a claim names exactly one of provenance_id and proposal_item")
        # Canonical timestamps are fixed-width UTC: they order as text.
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to < self.valid_from
        ):
            raise ValueError("valid_to must not precede valid_from")
        return self

    @classmethod
    def of_provenance(
        cls, entry: ProvenanceEntry, value: JsonValue, *, chosen: str | None = None
    ) -> Claim:
        """The claim of a value written on a branch under ``entry``."""
        return cls(
            claim_id=entry.id,
            value=value,
            source=source_identity(entry.origin, chosen=chosen),
            provenance_id=entry.id,
            valid_from=entry.valid_from,
            valid_to=entry.valid_to,
            at=entry.at,
        )

    @classmethod
    def of_proposal_item(
        cls,
        proposal_id: str,
        item_id: str,
        value: JsonValue,
        *,
        source: str | None,
        at: object,
        evidence: dict[str, JsonValue] | None = None,
        valid_from: object = None,
        valid_to: object = None,
    ) -> Claim:
        """The claim of a value only proposed by a Proposal item (extraction: the source is
        ``document:{id}``)."""
        return cls(
            claim_id=f"{proposal_id}/{item_id}",
            value=value,
            source=source,
            proposal_item=ProposalItemRef(proposal_id=proposal_id, item_id=item_id),
            evidence=evidence,
            valid_from=valid_from,
            valid_to=valid_to,
            at=at,
        )

    @property
    def has_period(self) -> bool:
        return self.valid_from is not None or self.valid_to is not None

    @property
    def written_at(self) -> datetime:
        return datetime.fromisoformat(self.at)

    def document(self) -> dict[str, Any]:
        """JSON form, unset members left out (the stored form of ``claims``)."""
        return self.model_dump(mode="json", exclude_none=True)


__all__ = [
    "Claim",
    "IdentityKind",
    "ProposalItemRef",
    "identity",
    "source_identity",
    "value_hash",
]
