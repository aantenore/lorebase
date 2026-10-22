"""Versioning core (design 7.1): Domains, objects, tree nodes, Revisions, changesets, refs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from lorebase_store.models.base import (
    H32,
    HASH_PARTITIONED,
    Base,
    DomainKey,
    domain_fk,
    h32_check,
)
from lorebase_store.models.enums import MATERIALIZATION, REF_KIND, Materialization, RefKind


class Domain(Base):
    """A Domain: the isolation boundary for schema, data, grants and history."""

    __tablename__ = "domains"

    id: Mapped[str] = mapped_column(primary_key=True)
    display_name: Mapped[str]
    description: Mapped[str | None]
    default_branch: Mapped[str] = mapped_column(server_default=text("'main'"))
    leaf_size: Mapped[int] = mapped_column(Integer)
    settings: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime]
    created_by: Mapped[str]


class StoredObject(Base):
    """Content-addressed schema element, Record or Link, stored once per Domain."""

    __tablename__ = "objects"
    __table_args__ = (h32_check("hash"), domain_fk(), HASH_PARTITIONED)

    domain_id: Mapped[DomainKey]
    hash: Mapped[H32] = mapped_column(primary_key=True)
    object_type: Mapped[int] = mapped_column(SmallInteger)
    body: Mapped[dict[str, Any]]
    size: Mapped[int] = mapped_column(Integer)


class TreeNode(Base):
    """Binary HAMT node; `body` uses lz4 TOAST compression (set by migration DDL)."""

    __tablename__ = "tree_nodes"
    __table_args__ = (h32_check("hash"), domain_fk(), HASH_PARTITIONED)

    domain_id: Mapped[DomainKey]
    hash: Mapped[H32] = mapped_column(primary_key=True)
    body: Mapped[bytes]


class Revision(Base):
    """Immutable Revision with its generation number (design 6.4)."""

    __tablename__ = "revisions"
    __table_args__ = (h32_check("id"), h32_check("root"), domain_fk())

    domain_id: Mapped[DomainKey]
    id: Mapped[H32] = mapped_column(primary_key=True)
    root: Mapped[H32]
    generation: Mapped[int] = mapped_column(BigInteger)
    author: Mapped[str]
    on_behalf_of: Mapped[str | None]
    message: Mapped[str]
    origin: Mapped[dict[str, Any]]
    stats: Mapped[dict[str, Any]]
    created_at: Mapped[datetime]


class RevisionParent(Base):
    """Ordered parent edge of a Revision; position 0 is the first parent."""

    __tablename__ = "revision_parents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["domain_id", "revision_id"],
            ["revisions.domain_id", "revisions.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["domain_id", "parent_id"],
            ["revisions.domain_id", "revisions.id"],
            deferrable=True,
            initially="DEFERRED",
        ),
        Index(None, "domain_id", "parent_id"),
    )

    domain_id: Mapped[DomainKey]
    revision_id: Mapped[H32] = mapped_column(primary_key=True)
    position: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    parent_id: Mapped[H32]


class ChangesetEntry(Base):
    """One element change of a Revision against its first parent (design 6.3)."""

    __tablename__ = "changesets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["domain_id", "revision_id"],
            ["revisions.domain_id", "revisions.id"],
            ondelete="CASCADE",
        ),
    )

    domain_id: Mapped[DomainKey]
    revision_id: Mapped[H32] = mapped_column(primary_key=True)
    partition: Mapped[str] = mapped_column(primary_key=True)
    element_key: Mapped[str] = mapped_column(primary_key=True)
    old_hash: Mapped[H32 | None]
    new_hash: Mapped[H32 | None]
    provenance: Mapped[dict[str, Any] | None]


class Provenance(Base):
    """Where a value came from: origin, actor and time."""

    __tablename__ = "provenance"
    __table_args__ = (domain_fk(),)

    domain_id: Mapped[DomainKey]
    id: Mapped[str] = mapped_column(primary_key=True)
    origin_type: Mapped[str]
    origin_ref: Mapped[dict[str, Any]] = mapped_column(JSONB)
    actor: Mapped[str]
    at: Mapped[datetime]


class Ref(Base):
    """Branch or tag head; moved by compare-and-swap on `(head, version)`.

    An overlay branch (design 6.11) names its base branch and the base head it was forked
    from (`base_revision`, with its generation): it stores only the rows it changed and reads
    the base rows as of that point. The three columns are set exactly for overlays.

    `deleted_at` marks a branch whose deletion is in progress (revision 0017): it is hidden
    from every read and write while a `store.branch_delete` job removes its rows in batches,
    and its name stays taken until the job deletes the ref.
    """

    __tablename__ = "refs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["domain_id", "head"],
            ["revisions.domain_id", "revisions.id"],
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["domain_id", "base_branch"],
            ["refs.domain_id", "refs.name"],
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["domain_id", "base_revision"],
            ["revisions.domain_id", "revisions.id"],
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "(materialization = 'overlay') = (base_branch IS NOT NULL "
            "AND base_revision IS NOT NULL AND base_generation IS NOT NULL)",
            name="overlay_base",
        ),
        Index(
            "ix_refs_domain_id_base_branch",
            "domain_id",
            "base_branch",
            postgresql_where=text("base_branch IS NOT NULL"),
        ),
        domain_fk(),
    )

    domain_id: Mapped[DomainKey]
    name: Mapped[str] = mapped_column(primary_key=True)
    kind: Mapped[RefKind] = mapped_column(REF_KIND)
    head: Mapped[H32]
    protected: Mapped[bool] = mapped_column(server_default=text("false"))
    materialization: Mapped[Materialization] = mapped_column(MATERIALIZATION)
    version: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime]
    created_by: Mapped[str]
    base_branch: Mapped[str | None]
    base_revision: Mapped[H32 | None]
    base_generation: Mapped[int | None] = mapped_column(BigInteger)
    deleted_at: Mapped[datetime | None]


class RefLogEntry(Base):
    """Every head movement of a ref, kept after the ref is deleted."""

    __tablename__ = "ref_log"
    __table_args__ = (domain_fk(),)

    domain_id: Mapped[DomainKey]
    ref_name: Mapped[str] = mapped_column(primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    old_head: Mapped[H32 | None]
    new_head: Mapped[H32]
    reason: Mapped[str]
    actor: Mapped[str]
    at: Mapped[datetime]
