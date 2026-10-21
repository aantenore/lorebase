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
        # search vector and trigram title indexes.
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
        Index(
            "ix_mat_records_branch_title",
            "domain_id",
            "branch",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
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
    """Vector of an object for one model; shared by all branches.

    The column is dimensionless so models of different sizes coexist; `dims` must match the
    vector and selects the per-dimension HNSW index.
    """

    __tablename__ = "embeddings"
    __table_args__ = (
        CheckConstraint("vector_dims(embedding) = dims", name="dims_match"),
        domain_fk(),
    )

    domain_id: Mapped[DomainKey]
    object_hash: Mapped[H32] = mapped_column(primary_key=True)
    model: Mapped[str] = mapped_column(primary_key=True)
    dims: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(VECTOR())
