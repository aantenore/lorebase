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
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    erased_at: Mapped[datetime | None]
