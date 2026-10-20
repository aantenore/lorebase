"""Source runs, documents, extraction and the personal data vault (design 7.4, 9, 13.5)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKeyConstraint, Index, Integer, text
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
    """Uploaded document; the bytes live in the blob store at `blob_ref`."""

    __tablename__ = "documents"
    __table_args__ = (h32_check("sha256"), domain_fk(), Index(None, "domain_id", "sha256"))

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
    __table_args__ = (domain_fk(),)

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


class ExtractionCacheEntry(Base):
    """Model output cached by content key; holds no Domain data beyond the key."""

    __tablename__ = "extraction_cache"
    __table_args__ = (h32_check("cache_key"),)

    cache_key: Mapped[H32] = mapped_column(primary_key=True)
    model: Mapped[str]
    output: Mapped[dict[str, Any]]
    usage: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class VaultEntry(Base):
    """Encrypted personal value referenced from Record objects; erasure clears it."""

    __tablename__ = "vault"
    __table_args__ = (domain_fk(),)

    domain_id: Mapped[DomainKey]
    id: Mapped[str] = mapped_column(primary_key=True)
    value_ciphertext: Mapped[bytes | None]
    key_id: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    erased_at: Mapped[datetime | None]
