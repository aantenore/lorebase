"""Initial schema (design 7).

Besides the tables, this revision installs what model metadata cannot express:

- extensions `pg_trgm` and `vector` (created if missing; never dropped on downgrade because
  an administrator may have installed them for other users);
- native enum types, created once and shared by several tables;
- `store.layout.hash_partitions` hash partitions on `domain_id`;
- lz4 TOAST compression of `tree_nodes.body`;
- the HNSW index of `embeddings` for `store.embeddings.dims`, with the build parameters of
  `store.storage` (`hnsw_m`, `hnsw_ef_construction`);
- row-level security on `domain_id` keyed to the transaction setting `lorebase.domain_ids`
  (comma-separated Domain ids), bypassed when `lorebase.bypass_rls` is `on`.

Settings are read when the revision runs; this file never changes after release.

Revision ID: 0001
Revises:
Create Date: 2026-10-20 18:41:41.275934
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXTENSIONS = ("pg_trgm", "vector")

ENUMS = {
    "ref_kind": ("branch", "tag"),
    "materialization": ("full", "overlay", "none"),
    "proposal_origin": ("manual", "source_sync", "extraction", "operation"),
    "proposal_status": ("open", "merged", "closed"),
    "review_verdict": ("approve", "request_changes", "comment"),
    "item_decision": ("pending", "accepted", "rejected"),
    "merge_attempt_status": ("conflicted", "stale", "resolved", "merged", "abandoned"),
    "principal_type": ("user", "service", "api_key"),
    "grant_scope": ("domain", "kind", "operation", "branch"),
    "invocation_status": ("pending", "committed", "proposed", "rejected", "dry_run", "failed"),
    "job_status": ("queued", "running", "succeeded", "failed", "cancelled"),
    "run_status": ("queued", "running", "succeeded", "failed", "cancelled"),
}

HASH_PARTITIONED = ("objects", "tree_nodes", "mat_records", "mat_links")

LZ4_COLUMNS = (("tree_nodes", "body"),)

DOMAIN_SCOPED = (
    "objects",
    "tree_nodes",
    "revisions",
    "revision_parents",
    "changesets",
    "provenance",
    "refs",
    "ref_log",
    "mat_schema",
    "mat_records",
    "mat_links",
    "mat_history",
    "embeddings",
    "proposals",
    "proposal_reviews",
    "proposal_items",
    "merge_attempts",
    "conflicts",
    "conflict_resolutions",
    "grants",
    "operation_invocations",
    "audit_log",
    "outbox",
    "jobs",
    "source_runs",
    "documents",
    "document_chunks",
    "extraction_jobs",
    "vault",
)

DOMAIN_VISIBLE = (
    "current_setting('lorebase.bypass_rls', true) = 'on' "
    "OR domain_id = ANY (string_to_array(current_setting('lorebase.domain_ids', true), ','))"
)


def _settings() -> tuple[int, int]:
    """(hash partitions, embedding dimension) from the `store` settings."""
    config = op.get_context().config
    if config is None or "store_settings" not in config.attributes:
        raise RuntimeError("run this migration through the Lorebase Alembic environment")
    store = config.attributes["store_settings"]
    return store.layout.hash_partitions, store.embeddings.dims


def _hnsw_options() -> str:
    """`WITH (...)` of the HNSW index from `store.storage.hnsw_m` and `hnsw_ef_construction`
    (null: pgvector's default)."""
    storage = op.get_context().config.attributes["store_settings"].storage
    options = [
        f"{name} = {value}"
        for name, value in (
            ("m", storage.hnsw_m),
            ("ef_construction", storage.hnsw_ef_construction),
        )
        if value is not None
    ]
    return f" WITH ({', '.join(options)})" if options else ""


def _create_partitions(partitions: int) -> None:
    for table in HASH_PARTITIONED:
        for remainder in range(partitions):
            op.execute(
                f"CREATE TABLE {table}_p{remainder} PARTITION OF {table} "
                f"FOR VALUES WITH (MODULUS {partitions}, REMAINDER {remainder})"
            )


def _with_partitions(table: str, partitions: int) -> list[str]:
    """`table` and, when it is hash-partitioned, each of its partitions."""
    if table not in HASH_PARTITIONED:
        return [table]
    return [table, *(f"{table}_p{remainder}" for remainder in range(partitions))]


def _compress(partitions: int) -> None:
    # Set on every partition: ALTER on a partitioned parent does not reach existing partitions.
    for table, column in LZ4_COLUMNS:
        for target in _with_partitions(table, partitions):
            op.execute(f"ALTER TABLE {target} ALTER COLUMN {column} SET COMPRESSION lz4")


def _enable_row_security(partitions: int) -> None:
    # Partitions get their own policy: querying a partition directly skips the parent's.
    for table in DOMAIN_SCOPED:
        for target in _with_partitions(table, partitions):
            op.execute(f"ALTER TABLE {target} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {target} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY domain_isolation ON {target} "
                f"USING ({DOMAIN_VISIBLE}) WITH CHECK ({DOMAIN_VISIBLE})"
            )


def upgrade() -> None:
    partitions, dims = _settings()
    for extension in EXTENSIONS:
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")
    bind = op.get_bind()
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=False)

    op.create_table(
        "audit_log",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("domain_id", sa.Text(), nullable=True),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("prev_hash", sa.LargeBinary(), nullable=True),
        sa.Column("entry_hash", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint(
            "octet_length(entry_hash) = 32", name=op.f("ck_audit_log_entry_hash_h32")
        ),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_audit_log")),
    )
    op.create_index(
        op.f("ix_audit_log_domain_id_seq"), "audit_log", ["domain_id", "seq"], unique=False
    )
    op.create_index(op.f("ix_audit_log_entry_hash"), "audit_log", ["entry_hash"], unique=True)
    op.create_table(
        "domains",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("default_branch", sa.Text(), server_default=sa.text("'main'"), nullable=False),
        sa.Column("leaf_size", sa.Integer(), nullable=False),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_domains")),
    )
    op.create_table(
        "extraction_cache",
        sa.Column("cache_key", sa.LargeBinary(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(cache_key) = 32", name=op.f("ck_extraction_cache_cache_key_h32")
        ),
        sa.PrimaryKeyConstraint("cache_key", name=op.f("pk_extraction_cache")),
    )
    op.create_table(
        "groups",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("external_group", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_groups")),
    )
    op.create_index(
        op.f("ix_groups_external_group"),
        "groups",
        ["external_group"],
        unique=True,
        postgresql_where=sa.text("external_group IS NOT NULL"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("domain_id", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="job_status", create_type=False),
            server_default=sa.text("'queued'"),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(
        "ix_jobs_claimable",
        "jobs",
        [sa.literal_column("priority DESC"), "run_after"],
        unique=False,
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(op.f("ix_jobs_domain_id_type"), "jobs", ["domain_id", "type"], unique=False)
    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
    )
    op.create_index(
        "ix_outbox_undelivered",
        "outbox",
        ["id"],
        unique=False,
        postgresql_where=sa.text("delivered_at IS NULL"),
    )
    op.create_table(
        "principals",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column(
            "type", postgresql.ENUM(name="principal_type", create_type=False), nullable=False
        ),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("external_subject", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_principals")),
    )
    op.create_index(
        op.f("ix_principals_external_subject"),
        "principals",
        ["external_subject"],
        unique=True,
        postgresql_where=sa.text("external_subject IS NOT NULL"),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.Column("hash", sa.LargeBinary(), nullable=False),
        sa.Column(
            "scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("octet_length(hash) = 32", name=op.f("ck_api_keys_hash_h32")),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_api_keys_principal_id_principals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("prefix", name=op.f("uq_api_keys_prefix")),
    )
    op.create_index(op.f("ix_api_keys_principal_id"), "api_keys", ["principal_id"], unique=False)
    op.create_table(
        "documents",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("sha256", sa.LargeBinary(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("blob_ref", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.LargeBinary(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.CheckConstraint("octet_length(sha256) = 32", name=op.f("ck_documents_sha256_h32")),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_documents_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "id", name=op.f("pk_documents")),
    )
    op.create_index(
        op.f("ix_documents_domain_id_sha256"), "documents", ["domain_id", "sha256"], unique=False
    )
    op.create_table(
        "embeddings",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("object_hash", sa.LargeBinary(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("dims", sa.Integer(), nullable=False),
        sa.Column("embedding", VECTOR(), nullable=False),
        sa.CheckConstraint("vector_dims(embedding) = dims", name=op.f("ck_embeddings_dims_match")),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_embeddings_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "object_hash", "model", name=op.f("pk_embeddings")),
    )
    op.create_table(
        "extraction_jobs",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("documents", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("schema_hash", sa.LargeBinary(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("status", postgresql.ENUM(name="run_status", create_type=False), nullable=False),
        sa.Column(
            "budget",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "usage",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("proposal_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_extraction_jobs_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "id", name=op.f("pk_extraction_jobs")),
    )
    op.create_table(
        "grants",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "scope_type", postgresql.ENUM(name="grant_scope", create_type=False), nullable=False
        ),
        sa.Column("scope_ref", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_grants_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_grants")),
    )
    op.create_index(
        op.f("ix_grants_domain_id_subject"), "grants", ["domain_id", "subject"], unique=False
    )
    op.create_index(op.f("ix_grants_subject"), "grants", ["subject"], unique=False)
    op.create_table(
        "group_members",
        sa.Column("group_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name=op.f("fk_group_members_group_id_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_group_members_principal_id_principals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("group_id", "principal_id", name=op.f("pk_group_members")),
    )
    op.create_index(
        op.f("ix_group_members_principal_id"), "group_members", ["principal_id"], unique=False
    )
    op.create_table(
        "objects",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("hash", sa.LargeBinary(), nullable=False),
        sa.Column("object_type", sa.SmallInteger(), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.CheckConstraint("octet_length(hash) = 32", name=op.f("ck_objects_hash_h32")),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_objects_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "hash", name=op.f("pk_objects")),
        postgresql_partition_by="HASH (domain_id)",
    )
    op.create_table(
        "operation_invocations",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("principal", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status", postgresql.ENUM(name="invocation_status", create_type=False), nullable=False
        ),
        sa.Column("revision_id", sa.LargeBinary(), nullable=True),
        sa.Column("proposal_id", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_operation_invocations_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "id", name=op.f("pk_operation_invocations")),
    )
    op.create_index(
        "uq_operation_invocations_idempotency",
        "operation_invocations",
        ["domain_id", "principal", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_table(
        "proposals",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("target_ref", sa.Text(), nullable=False),
        sa.Column(
            "origin", postgresql.ENUM(name="proposal_origin", create_type=False), nullable=False
        ),
        sa.Column(
            "status", postgresql.ENUM(name="proposal_status", create_type=False), nullable=False
        ),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("merged_revision", sa.LargeBinary(), nullable=True),
        sa.Column("checks", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("policies", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_proposals_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "id", name=op.f("pk_proposals")),
    )
    op.create_index(
        op.f("ix_proposals_domain_id_target_ref_status"),
        "proposals",
        ["domain_id", "target_ref", "status"],
        unique=False,
    )
    op.create_table(
        "provenance",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("origin_type", sa.Text(), nullable=False),
        sa.Column("origin_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_provenance_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "id", name=op.f("pk_provenance")),
    )
    op.create_table(
        "ref_log",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("ref_name", sa.Text(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("old_head", sa.LargeBinary(), nullable=True),
        sa.Column("new_head", sa.LargeBinary(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_ref_log_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "ref_name", "seq", name=op.f("pk_ref_log")),
    )
    op.create_table(
        "revisions",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("id", sa.LargeBinary(), nullable=False),
        sa.Column("root", sa.LargeBinary(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("on_behalf_of", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("origin", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("octet_length(id) = 32", name=op.f("ck_revisions_id_h32")),
        sa.CheckConstraint("octet_length(root) = 32", name=op.f("ck_revisions_root_h32")),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_revisions_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "id", name=op.f("pk_revisions")),
    )
    op.create_table(
        "source_runs",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("status", postgresql.ENUM(name="run_status", create_type=False), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "stats",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("revision_id", sa.LargeBinary(), nullable=True),
        sa.Column("proposal_id", sa.Text(), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_source_runs_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "id", name=op.f("pk_source_runs")),
    )
    op.create_index(
        op.f("ix_source_runs_domain_id_source_id_started_at"),
        "source_runs",
        ["domain_id", "source_id", "started_at"],
        unique=False,
    )
    op.create_table(
        "tree_nodes",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("hash", sa.LargeBinary(), nullable=False),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("octet_length(hash) = 32", name=op.f("ck_tree_nodes_hash_h32")),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_tree_nodes_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "hash", name=op.f("pk_tree_nodes")),
        postgresql_partition_by="HASH (domain_id)",
    )
    op.create_table(
        "vault",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("value_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("key_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_vault_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "id", name=op.f("pk_vault")),
    )
    op.create_table(
        "changesets",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("revision_id", sa.LargeBinary(), nullable=False),
        sa.Column("partition", sa.Text(), nullable=False),
        sa.Column("element_key", sa.Text(), nullable=False),
        sa.Column("old_hash", sa.LargeBinary(), nullable=True),
        sa.Column("new_hash", sa.LargeBinary(), nullable=True),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["domain_id", "revision_id"],
            ["revisions.domain_id", "revisions.id"],
            name=op.f("fk_changesets_domain_id_revision_id_revisions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "domain_id", "revision_id", "partition", "element_key", name=op.f("pk_changesets")
        ),
    )
    op.create_table(
        "conflict_resolutions",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("resolution", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("resolved_by", sa.Text(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id", "proposal_id"],
            ["proposals.domain_id", "proposals.id"],
            name=op.f("fk_conflict_resolutions_domain_id_proposal_id_proposals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "domain_id", "proposal_id", "fingerprint", name=op.f("pk_conflict_resolutions")
        ),
    )
    op.create_table(
        "document_chunks",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("chunk_hash", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id", "document_id"],
            ["documents.domain_id", "documents.id"],
            name=op.f("fk_document_chunks_domain_id_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "document_id", "seq", name=op.f("pk_document_chunks")),
    )
    op.create_table(
        "merge_attempts",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Text(), nullable=False),
        sa.Column("base", sa.LargeBinary(), nullable=False),
        sa.Column("ours", sa.LargeBinary(), nullable=False),
        sa.Column("theirs", sa.LargeBinary(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="merge_attempt_status", create_type=False),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id", "proposal_id"],
            ["proposals.domain_id", "proposals.id"],
            name=op.f("fk_merge_attempts_domain_id_proposal_id_proposals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "id", name=op.f("pk_merge_attempts")),
    )
    op.create_index(
        op.f("ix_merge_attempts_domain_id_proposal_id"),
        "merge_attempts",
        ["domain_id", "proposal_id"],
        unique=False,
    )
    op.create_table(
        "proposal_items",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Text(), nullable=False),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("partition", sa.Text(), nullable=False),
        sa.Column("element_key", sa.Text(), nullable=False),
        sa.Column("attribute_id", sa.Text(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("confidence", sa.REAL(), nullable=True),
        sa.Column(
            "decision", postgresql.ENUM(name="item_decision", create_type=False), nullable=False
        ),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["domain_id", "proposal_id"],
            ["proposals.domain_id", "proposals.id"],
            name=op.f("fk_proposal_items_domain_id_proposal_id_proposals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "domain_id", "proposal_id", "item_id", name=op.f("pk_proposal_items")
        ),
    )
    op.create_table(
        "proposal_reviews",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Text(), nullable=False),
        sa.Column("reviewer", sa.Text(), nullable=False),
        sa.Column(
            "verdict", postgresql.ENUM(name="review_verdict", create_type=False), nullable=False
        ),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id", "proposal_id"],
            ["proposals.domain_id", "proposals.id"],
            name=op.f("fk_proposal_reviews_domain_id_proposal_id_proposals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "id", name=op.f("pk_proposal_reviews")),
    )
    op.create_index(
        op.f("ix_proposal_reviews_domain_id_proposal_id"),
        "proposal_reviews",
        ["domain_id", "proposal_id"],
        unique=False,
    )
    op.create_table(
        "refs",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", postgresql.ENUM(name="ref_kind", create_type=False), nullable=False),
        sa.Column("head", sa.LargeBinary(), nullable=False),
        sa.Column("protected", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "materialization",
            postgresql.ENUM(name="materialization", create_type=False),
            nullable=False,
        ),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id", "head"],
            ["revisions.domain_id", "revisions.id"],
            name=op.f("fk_refs_domain_id_head_revisions"),
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_refs_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "name", name=op.f("pk_refs")),
    )
    op.create_table(
        "revision_parents",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("revision_id", sa.LargeBinary(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("parent_id", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id", "parent_id"],
            ["revisions.domain_id", "revisions.id"],
            name=op.f("fk_revision_parents_domain_id_parent_id_revisions"),
        ),
        sa.ForeignKeyConstraint(
            ["domain_id", "revision_id"],
            ["revisions.domain_id", "revisions.id"],
            name=op.f("fk_revision_parents_domain_id_revision_id_revisions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "domain_id", "revision_id", "position", name=op.f("pk_revision_parents")
        ),
    )
    op.create_index(
        op.f("ix_revision_parents_domain_id_parent_id"),
        "revision_parents",
        ["domain_id", "parent_id"],
        unique=False,
    )
    op.create_table(
        "conflicts",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("attempt_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("partition", sa.Text(), nullable=False),
        sa.Column("element_key", sa.Text(), nullable=True),
        sa.Column("attribute_id", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("resolution", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["domain_id", "attempt_id"],
            ["merge_attempts.domain_id", "merge_attempts.id"],
            name=op.f("fk_conflicts_domain_id_attempt_id_merge_attempts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "attempt_id", "id", name=op.f("pk_conflicts")),
        sa.UniqueConstraint(
            "domain_id",
            "attempt_id",
            "fingerprint",
            name=op.f("uq_conflicts_domain_id_attempt_id_fingerprint"),
        ),
    )
    op.create_table(
        "mat_history",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("partition", sa.Text(), nullable=False),
        sa.Column("element_key", sa.Text(), nullable=False),
        sa.Column("revision_id", sa.LargeBinary(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("old_hash", sa.LargeBinary(), nullable=True),
        sa.Column("new_hash", sa.LargeBinary(), nullable=True),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["domain_id", "branch"],
            ["refs.domain_id", "refs.name"],
            name=op.f("fk_mat_history_domain_id_branch_refs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "domain_id",
            "branch",
            "partition",
            "element_key",
            "revision_id",
            name=op.f("pk_mat_history"),
        ),
    )
    op.create_table(
        "mat_links",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("relation_id", sa.Text(), nullable=False),
        sa.Column("from_key", sa.Text(), nullable=False),
        sa.Column("to_key", sa.Text(), nullable=False),
        sa.Column(
            "values",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("object_hash", sa.LargeBinary(), nullable=False),
        sa.Column("lineage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id", "branch"],
            ["refs.domain_id", "refs.name"],
            name=op.f("fk_mat_links_domain_id_branch_refs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "domain_id", "branch", "relation_id", "from_key", "to_key", name=op.f("pk_mat_links")
        ),
        postgresql_partition_by="HASH (domain_id)",
    )
    op.create_index(
        "ix_mat_links_reverse",
        "mat_links",
        ["domain_id", "branch", "relation_id", "to_key"],
        unique=False,
    )
    op.create_table(
        "mat_records",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("kind_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("values", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "derived",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("search", postgresql.TSVECTOR(), nullable=True),
        sa.Column("object_hash", sa.LargeBinary(), nullable=False),
        sa.Column("lineage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_revision", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id", "branch"],
            ["refs.domain_id", "refs.name"],
            name=op.f("fk_mat_records_domain_id_branch_refs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "domain_id", "branch", "kind_id", "key", name=op.f("pk_mat_records")
        ),
        postgresql_partition_by="HASH (domain_id)",
    )
    op.create_index(
        "ix_mat_records_search", "mat_records", ["search"], unique=False, postgresql_using="gin"
    )
    op.create_index(
        "ix_mat_records_title",
        "mat_records",
        ["title"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_mat_records_values",
        "mat_records",
        ["values"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"values": "jsonb_path_ops"},
    )
    op.create_table(
        "mat_schema",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("schema_hash", sa.LargeBinary(), nullable=False),
        sa.Column("compiled", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id", "branch"],
            ["refs.domain_id", "refs.name"],
            name=op.f("fk_mat_schema_domain_id_branch_refs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "branch", name=op.f("pk_mat_schema")),
    )

    _create_partitions(partitions)
    _compress(partitions)
    op.execute(
        f"CREATE INDEX embeddings_hnsw_{dims} ON embeddings "
        f"USING hnsw ((embedding::vector({dims})) vector_cosine_ops){_hnsw_options()} "
        f"WHERE dims = {dims}"
    )
    _enable_row_security(partitions)


def downgrade() -> None:
    # Indexes, partitions and policies go with their tables.
    op.drop_table("mat_schema")
    op.drop_table("mat_records")
    op.drop_table("mat_links")
    op.drop_table("mat_history")
    op.drop_table("conflicts")
    op.drop_table("revision_parents")
    op.drop_table("refs")
    op.drop_table("proposal_reviews")
    op.drop_table("proposal_items")
    op.drop_table("merge_attempts")
    op.drop_table("document_chunks")
    op.drop_table("conflict_resolutions")
    op.drop_table("changesets")
    op.drop_table("vault")
    op.drop_table("tree_nodes")
    op.drop_table("source_runs")
    op.drop_table("revisions")
    op.drop_table("ref_log")
    op.drop_table("provenance")
    op.drop_table("proposals")
    op.drop_table("operation_invocations")
    op.drop_table("objects")
    op.drop_table("group_members")
    op.drop_table("grants")
    op.drop_table("extraction_jobs")
    op.drop_table("embeddings")
    op.drop_table("documents")
    op.drop_table("api_keys")
    op.drop_table("principals")
    op.drop_table("outbox")
    op.drop_table("jobs")
    op.drop_table("groups")
    op.drop_table("extraction_cache")
    op.drop_table("domains")
    op.drop_table("audit_log")
    bind = op.get_bind()
    for name in reversed(ENUMS):
        postgresql.ENUM(name=name).drop(bind, checkfirst=False)
