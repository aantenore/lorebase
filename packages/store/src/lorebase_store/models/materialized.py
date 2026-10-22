"""Materialized heads (design 7.2): compiled schema, Records, Links, history, embeddings.

Per-Attribute expression indexes on `mat_records` and per-dimension HNSW indexes on
`embeddings` depend on configuration and schema; they are created outside this metadata.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import BigInteger, CheckConstraint, ForeignKeyConstraint, Index, Integer, text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from lorebase_store.models.base import H32, HASH_PARTITIONED, Base, DomainKey, domain_fk


def _branch_fk() -> ForeignKeyConstraint:
    """Materialized rows belong to a ref and go when the branch is deleted."""
    return ForeignKeyConstraint(
        ["domain_id", "branch"], ["refs.domain_id", "refs.name"], ondelete="CASCADE"
    )


class MatSchema(Base):
    """Compiled schema at a branch head."""

    __tablename__ = "mat_schema"
    __table_args__ = (_branch_fk(),)

    domain_id: Mapped[DomainKey]
    branch: Mapped[str] = mapped_column(primary_key=True)
    schema_hash: Mapped[H32]
    compiled: Mapped[dict[str, Any]]


class MatRecord(Base):
    """One Record at a branch head; `values` is keyed by Attribute id.

    `revision_generation` is the generation of the Revision that last wrote the row and
    `deleted` marks a tombstone, which only overlay branches hold (store schema notes).
    """

    __tablename__ = "mat_records"
    __table_args__ = (
        _branch_fk(),
        # btree_gin (revision 0011): a search's Domain and branch are index conditions of the
        # search vector index.
        Index(
            "ix_mat_records_branch_search",
            "domain_id",
            "branch",
            "search",
            postgresql_using="gin",
        ),
        Index(
            "ix_mat_records_values",
            "values",
            postgresql_using="gin",
            postgresql_ops={"values": "jsonb_path_ops"},
        ),
        HASH_PARTITIONED,
    )

    domain_id: Mapped[DomainKey]
    branch: Mapped[str] = mapped_column(primary_key=True)
    kind_id: Mapped[str] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    values: Mapped[dict[str, Any]]
    derived: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    title: Mapped[str | None]
    search: Mapped[str | None] = mapped_column(TSVECTOR)
    object_hash: Mapped[H32]
    lineage: Mapped[dict[str, Any]]
    updated_revision: Mapped[H32]
    updated_at: Mapped[datetime]
    revision_generation: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    deleted: Mapped[bool] = mapped_column(server_default=text("false"))


class MatLink(Base):
    """One Link at a branch head, indexed in both directions."""

    __tablename__ = "mat_links"
    __table_args__ = (
        _branch_fk(),
        Index("ix_mat_links_reverse", "domain_id", "branch", "relation_id", "to_key"),
        HASH_PARTITIONED,
    )

    domain_id: Mapped[DomainKey]
    branch: Mapped[str] = mapped_column(primary_key=True)
    relation_id: Mapped[str] = mapped_column(primary_key=True)
    from_key: Mapped[str] = mapped_column(primary_key=True)
    to_key: Mapped[str] = mapped_column(primary_key=True)
    values: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    object_hash: Mapped[H32]
    lineage: Mapped[dict[str, Any]]
    revision_generation: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    deleted: Mapped[bool] = mapped_column(server_default=text("false"))


class MatLexicon(Base):
    """Per Kind, the words of a branch's own search vectors and how many of its rows hold
    each (revision 0018): typo-tolerant search matches query words against them by trigrams.

    Kept by triggers on `mat_records`; words with a digit or longer than 64 characters are
    left out. An overlay holds the words of its own rows, and its readers add its base's.
    """

    __tablename__ = "mat_lexicon"
    __table_args__ = (
        _branch_fk(),
        Index(
            "ix_mat_lexicon_branch_word",
            "domain_id",
            "branch",
            "word",
            postgresql_using="gin",
            postgresql_ops={"word": "gin_trgm_ops"},
        ),
        Index("ix_mat_lexicon_spent", "domain_id", "branch", postgresql_where=text("records <= 0")),
    )

    domain_id: Mapped[DomainKey]
    branch: Mapped[str] = mapped_column(primary_key=True)
    kind_id: Mapped[str] = mapped_column(primary_key=True)
    word: Mapped[str] = mapped_column(primary_key=True)
    records: Mapped[int] = mapped_column(BigInteger)


class MatHistory(Base):
    """Per branch, per element change log along the first-parent chain."""

    __tablename__ = "mat_history"
    __table_args__ = (_branch_fk(),)

    domain_id: Mapped[DomainKey]
    branch: Mapped[str] = mapped_column(primary_key=True)
    partition: Mapped[str] = mapped_column(primary_key=True)
    element_key: Mapped[str] = mapped_column(primary_key=True)
    revision_id: Mapped[H32] = mapped_column(primary_key=True)
    at: Mapped[datetime]
    old_hash: Mapped[H32 | None]
    new_hash: Mapped[H32 | None]
    provenance: Mapped[dict[str, Any] | None]


class Embedding(Base):
    """Vector of an object for one model and embedding template; shared by all branches whose
    template of the object's Kind has that fingerprint.

    The column is dimensionless so models of different sizes coexist; `dims` must match the
    vector and selects the per-dimension HNSW index. `text_hash` is the SHA-256 of the text
    that was embedded (revision 0014): an object whose text did not change keeps its vector,
    and a new object with the text of another reuses that one's vector. `fingerprint` is the
    template fingerprint the text was rendered with (revision 0024); empty for a vector whose
    template could not be told when the column was added.
    """

    __tablename__ = "embeddings"
    __table_args__ = (
        CheckConstraint("vector_dims(embedding) = dims", name="dims_match"),
        domain_fk(),
        Index("ix_embeddings_domain_id_model_text_hash", "domain_id", "model", "text_hash"),
    )

    domain_id: Mapped[DomainKey]
    object_hash: Mapped[H32] = mapped_column(primary_key=True)
    model: Mapped[str] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(primary_key=True)
    dims: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(VECTOR())
    text_hash: Mapped[H32 | None]


class EmbeddingCursor(Base):
    """How far the embedding job got on a branch for one model (revision 0014).

    `head` is the branch head the last complete pass read and `fingerprints` the embedding
    template fingerprint of each embedded Kind at that head; a pass is skipped while both are
    unchanged, and Kinds whose fingerprint changed have every text rendered again.
    """

    __tablename__ = "embedding_cursors"
    __table_args__ = (_branch_fk(),)

    domain_id: Mapped[DomainKey]
    branch: Mapped[str] = mapped_column(primary_key=True)
    model: Mapped[str] = mapped_column(primary_key=True)
    head: Mapped[H32]
    fingerprints: Mapped[dict[str, Any]]
    updated_at: Mapped[datetime]


class BranchMaterialization(Base):
    """Progress of turning an overlay into a full copy in batches (revision 0018).

    One row per branch whose rows a `store.materialize` job copies: a copy branch created
    as an overlay, or an overlay promoted in the background. `phase` and `cursor` say where
    the next batch starts (the last key copied of `mat_records`, `mat_links` or
    `mat_history`); `base_branch` and `base_generation` are the fork point the copy reads.
    Rows go with their branch.
    """

    __tablename__ = "branch_materializations"
    __table_args__ = (
        _branch_fk(),
        CheckConstraint("status IN ('pending', 'running', 'done', 'failed')", name="status"),
        CheckConstraint("phase IN ('records', 'links', 'history', 'switch', 'done')", name="phase"),
    )

    domain_id: Mapped[DomainKey]
    branch: Mapped[str] = mapped_column(primary_key=True)
    status: Mapped[str]
    phase: Mapped[str]
    base_branch: Mapped[str]
    base_generation: Mapped[int] = mapped_column(BigInteger)
    cursor: Mapped[dict[str, Any] | None]
    rows_done: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    rows_total: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
