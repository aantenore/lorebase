"""Source runs, documents, extraction, schema suggestions and the personal data vault (design
7.4, 9, 13.5)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Float,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from lorebase_store.models.base import H32, Base, DomainKey, domain_fk, h32_check
from lorebase_store.models.enums import RUN_STATUS, RunStatus


class SourceRun(Base):
    """One synchronization run of a structured Source."""

    __tablename__ = "source_runs"
    __table_args__ = (domain_fk(), Index(None, "domain_id", "source_id", "started_at"))

    domain_id: Mapped[DomainKey]
    id: Mapped[str] = mapped_column(primary_key=True)
    source_id: Mapped[str]
    status: Mapped[RunStatus] = mapped_column(RUN_STATUS)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    stats: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    revision_id: Mapped[H32 | None]
    proposal_id: Mapped[str | None]
    error: Mapped[dict[str, Any] | None]


class SourceWritten(Base):
    """What a Source last wrote to a target branch, per element (design 9.1 "Corrections that
    stick"): the hash of each Attribute value (`written`, never the values), the Source branch
    Revision holding them (`revision_id`; `value_revisions` names an older one for the
    Attributes that Revision does not hold) and whether the Source last deleted the element.
    Advanced only in the transaction that merges the Source's Proposal."""

    __tablename__ = "source_written"
    __table_args__ = (domain_fk(), h32_check("revision_id"))

    domain_id: Mapped[DomainKey]
    source_id: Mapped[str] = mapped_column(primary_key=True)
    target_branch: Mapped[str] = mapped_column(primary_key=True)
    element: Mapped[str] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    written: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    revision_id: Mapped[H32 | None]
    value_revisions: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    deleted: Mapped[bool] = mapped_column(server_default=text("false"))
    run_id: Mapped[str]
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class SourceWrittenPending(Base):
    """The last-written state a Source run carries in its open Proposal, moved to
    `source_written` when the Proposal merges. `preserve` lists the Attributes whose merge
    base keeps the last-written value (local edits, design 9.1)."""

    __tablename__ = "source_written_pending"
    __table_args__ = (
        domain_fk(),
        h32_check("revision_id"),
        Index(None, "domain_id", "source_id", "target_branch"),
    )

    domain_id: Mapped[DomainKey]
    proposal_id: Mapped[str] = mapped_column(primary_key=True)
    element: Mapped[str] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    source_id: Mapped[str]
    target_branch: Mapped[str]
    written: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    revision_id: Mapped[H32 | None]
    value_revisions: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    deleted: Mapped[bool] = mapped_column(server_default=text("false"))
    preserve: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    run_id: Mapped[str]
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Document(Base):
    """Uploaded document; the bytes live in the blob store at `blob_ref`. One row per Domain
    and content hash."""

    __tablename__ = "documents"
    __table_args__ = (
        h32_check("sha256"),
        domain_fk(),
        Index(None, "domain_id", "sha256", unique=True),
    )

    domain_id: Mapped[DomainKey]
    id: Mapped[str] = mapped_column(primary_key=True)
    filename: Mapped[str]
    media_type: Mapped[str]
    sha256: Mapped[H32]
    size: Mapped[int] = mapped_column(BigInteger)
    blob_ref: Mapped[str]
    text_hash: Mapped[H32 | None]
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    created_by: Mapped[str]


class DocumentBlob(Base):
    """Document bytes of the Postgres blob store backend, addressed by SHA-256 per Domain."""

    __tablename__ = "document_blobs"
    __table_args__ = (h32_check("sha256"), domain_fk())

    domain_id: Mapped[DomainKey]
    sha256: Mapped[H32] = mapped_column(primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    size: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class DocumentChunk(Base):
    """Text span of a document used for extraction and evidence offsets."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["domain_id", "document_id"],
            ["documents.domain_id", "documents.id"],
            ondelete="CASCADE",
        ),
    )

    domain_id: Mapped[DomainKey]
    document_id: Mapped[str] = mapped_column(primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    text: Mapped[str]
    chunk_hash: Mapped[H32]


class ExtractionJob(Base):
    """Extraction over documents producing a Proposal."""

    __tablename__ = "extraction_jobs"
    __table_args__ = (domain_fk(), Index(None, "documents", postgresql_using="gin"))

    domain_id: Mapped[DomainKey]
    id: Mapped[str] = mapped_column(primary_key=True)
    documents: Mapped[Any] = mapped_column(JSONB)
    schema_hash: Mapped[H32]
    model: Mapped[str]
    prompt_version: Mapped[str]
    status: Mapped[RunStatus] = mapped_column(RUN_STATUS)
    budget: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    usage: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    proposal_id: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    created_by: Mapped[str | None]
    #: The extraction branch (`extract/<job id>`).
    branch: Mapped[str | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    stats: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    error: Mapped[dict[str, Any] | None]


class ExtractionCacheEntry(Base):
    """Model output cached by content key; holds no Domain data beyond the key."""

    __tablename__ = "extraction_cache"
    __table_args__ = (h32_check("cache_key"),)

    cache_key: Mapped[H32] = mapped_column(primary_key=True)
    model: Mapped[str]
    output: Mapped[dict[str, Any]]
    usage: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class InferenceEndpointMode(Base):
    """Structured output mode that a chat endpoint was found to need (`json_schema`: 0,
    `json_mode`: 1, `prompt`: 2), so a restarted process does not retry the stronger ones.

    `key` is the SHA-256 of the endpoint, model and output settings: no address is stored in
    the clear (a keyed digest when `inference.mode_memory_key` is set). Holds no Domain data.
    """

    __tablename__ = "inference_endpoint_modes"
    __table_args__ = (
        h32_check("key"),
        CheckConstraint("mode_index BETWEEN 0 AND 2", name="mode_index"),
    )

    key: Mapped[H32] = mapped_column(primary_key=True)
    mode_index: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class SchemaSuggestion(Base):
    """A new Kind, Attribute or Relation that extraction found outside the schema, aggregated
    over documents by `fingerprint` (`lorebase_core.infer.suggestions`).

    `observations` maps document ids to `{"n": count, "c": summed confidence}`, so extracting a
    document again replaces its share instead of adding to it; `support` is the sum of the
    counts. `examples` holds evidence (`document_id`, `chunk`, `start`, `end`, `quote`) and
    `samples` distinct sample values, both bounded. Accepting a suggestion opens a schema change
    Proposal (`proposal_id`, `branch`); nothing applies it to a schema by itself.
    """

    __tablename__ = "schema_suggestions"
    __table_args__ = (
        domain_fk(),
        Index(None, "domain_id", "fingerprint", unique=True),
        Index(None, "domain_id", "status", "support"),
        CheckConstraint(
            "suggestion_type IN ('kind', 'attribute', 'relation')", name="suggestion_type"
        ),
        CheckConstraint("status IN ('pending', 'accepted', 'rejected')", name="status"),
    )

    domain_id: Mapped[DomainKey]
    id: Mapped[str] = mapped_column(primary_key=True)
    fingerprint: Mapped[str]
    suggestion_type: Mapped[str]
    name: Mapped[str]
    #: Owner Kind of an Attribute, from Kind of a Relation: a Kind id or `+<KindName>`.
    kind_ref: Mapped[str | None]
    #: To Kind of a Relation.
    target_ref: Mapped[str | None]
    labels: Mapped[Any] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    support: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    confidence: Mapped[float] = mapped_column(Float, server_default=text("0"))
    observations: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    examples: Mapped[Any] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    samples: Mapped[Any] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    sources: Mapped[Any] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(server_default=text("'pending'"))
    decided_by: Mapped[str | None]
    decided_at: Mapped[datetime | None]
    proposal_id: Mapped[str | None]
    branch: Mapped[str | None]
    last_job_id: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class VaultEntry(Base):
    """Encrypted personal value referenced from Record objects by its token (`id`); erasure
    clears the ciphertext and the fingerprint and sets `erased_at` (`lorebase_store.vault`).

    The Kind, Record key and Attribute bind a token to the value it stands for; `fingerprint`
    (a keyed hash of the value without the Record key) finds the rows erasure by value erases.
    `key_id` names the encryption key and `fingerprint_key` the token key and scheme of the
    fingerprint (`<token_key_id>/<scheme>`, null for rows written before key rotation).
    """

    __tablename__ = "vault"
    __table_args__ = (
        domain_fk(),
        Index(None, "domain_id", "kind_id", "record_key"),
        Index(None, "domain_id", "kind_id", "attribute_id", "fingerprint"),
    )

    domain_id: Mapped[DomainKey]
    id: Mapped[str] = mapped_column(primary_key=True)
    kind_id: Mapped[str]
    attribute_id: Mapped[str]
    record_key: Mapped[str]
    fingerprint: Mapped[bytes | None]
    value_ciphertext: Mapped[bytes | None]
    key_id: Mapped[str | None]
    fingerprint_key: Mapped[str | None]
    moved_to: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    erased_at: Mapped[datetime | None]


class VaultTombstone(Base):
    """A value erased from the vault: the fingerprint of every value an erasure erased, of one
    Record (`record_key`) or of every Record of the Kind (null), so a later write of it is
    stored erased or refused (`vault.erased_values`). Holds no value."""

    __tablename__ = "vault_tombstones"
    __table_args__ = (
        domain_fk(),
        Index(None, "domain_id", "fingerprint"),
    )

    domain_id: Mapped[DomainKey]
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    kind_id: Mapped[str]
    attribute_id: Mapped[str]
    record_key: Mapped[str | None]
    fingerprint: Mapped[bytes]
    fingerprint_key: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class VaultLineage(Base):
    """A Record whose vaulted values were taken over from a Record of the same Kind under
    another key (a key change): erasing either erases the values of both."""

    __tablename__ = "vault_lineage"
    __table_args__ = (
        domain_fk(),
        Index(None, "domain_id", "kind_id", "previous_key"),
    )

    domain_id: Mapped[DomainKey]
    kind_id: Mapped[str] = mapped_column(primary_key=True)
    record_key: Mapped[str] = mapped_column(primary_key=True)
    previous_key: Mapped[str] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class InstallationSecret(Base):
    """Random secrets of the installation made by the migrations, such as the key of the keyed
    digests when no `vault.digest_key` is configured (`lorebase_store.digests`)."""

    __tablename__ = "installation_secrets"

    name: Mapped[str] = mapped_column(primary_key=True)
    secret: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
