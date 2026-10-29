"""Document versions and lifecycle (design 9.2 "Document versions and lifecycle", AC-35).

`documents` gains `source_key` (a stable identity: path, URL or external id), `version`,
`replaces` (the version a new one replaced), `status` (`active`, `superseded`, `retired`,
`purged`) and who retired or purged it when. Existing rows read as keyless, version 1,
`active`: the columns are added with defaults, no row is rewritten beyond that.

Indexes: the unique `(domain_id, sha256)` index becomes partial (keyless rows that are not
purged); keyed rows are unique by `(domain_id, source_key, version)`, at most one version of a
key is `active` (so the bytes of the current version are unique per key), and
`(domain_id, source_key, sha256)` finds a version by its bytes.

`extraction_cache` gains `chunk_hash`, the chunk an answer was computed from, so that purging a
document drops the cached answers of its chunks. Answers cached before keep it null.

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-30 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: str | Sequence[str] | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "documents"
SHA = "ix_documents_domain_id_sha256"
KEY_VERSION = "ix_documents_domain_id_source_key_version"
KEY_ACTIVE = "ix_documents_domain_id_source_key_active"
KEY_SHA = "ix_documents_domain_id_source_key_sha256"
CACHE = "extraction_cache"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("source_key", sa.Text(), nullable=True))
    op.add_column(
        TABLE, sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False)
    )
    op.add_column(TABLE, sa.Column("replaces", sa.Text(), nullable=True))
    op.add_column(
        TABLE, sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False)
    )
    for name in ("retired_at", "purged_at"):
        op.add_column(TABLE, sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    for name in ("retired_by", "purged_by"):
        op.add_column(TABLE, sa.Column(name, sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_documents_status"),
        TABLE,
        "status IN ('active', 'superseded', 'retired', 'purged')",
    )
    op.create_check_constraint(op.f("ck_documents_version"), TABLE, "version >= 1")
    op.drop_index(op.f(SHA), table_name=TABLE)
    op.create_index(
        op.f(SHA),
        TABLE,
        ["domain_id", "sha256"],
        unique=True,
        postgresql_where=sa.text("source_key IS NULL AND status <> 'purged'"),
    )
    op.create_index(
        op.f(KEY_VERSION),
        TABLE,
        ["domain_id", "source_key", "version"],
        unique=True,
        postgresql_where=sa.text("source_key IS NOT NULL"),
    )
    op.create_index(
        op.f(KEY_ACTIVE),
        TABLE,
        ["domain_id", "source_key"],
        unique=True,
        postgresql_where=sa.text("source_key IS NOT NULL AND status = 'active'"),
    )
    op.create_index(
        op.f(KEY_SHA),
        TABLE,
        ["domain_id", "source_key", "sha256"],
        postgresql_where=sa.text("source_key IS NOT NULL"),
    )
    op.add_column(CACHE, sa.Column("chunk_hash", sa.LargeBinary(), nullable=True))
    op.create_check_constraint(
        op.f("ck_extraction_cache_chunk_hash_h32"), CACHE, "octet_length(chunk_hash) = 32"
    )
    op.create_index(
        op.f("ix_extraction_cache_chunk_hash"),
        CACHE,
        ["chunk_hash"],
        postgresql_where=sa.text("chunk_hash IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_extraction_cache_chunk_hash"), table_name=CACHE)
    op.drop_constraint(op.f("ck_extraction_cache_chunk_hash_h32"), CACHE, type_="check")
    op.drop_column(CACHE, "chunk_hash")
    op.drop_index(op.f(KEY_SHA), table_name=TABLE)
    op.drop_index(op.f(KEY_ACTIVE), table_name=TABLE)
    op.drop_index(op.f(KEY_VERSION), table_name=TABLE)
    op.drop_index(op.f(SHA), table_name=TABLE)
    # Keyed versions and purged rows may share bytes with other rows; nothing is deleted here:
    # the downgrade stops instead, and the operator decides which rows to keep.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM documents GROUP BY domain_id, sha256 "
        "HAVING count(*) > 1) THEN RAISE EXCEPTION 'documents share bytes (versions or purged "
        "rows): remove the extra rows before downgrading below 0041'; END IF; END $$"
    )
    op.create_index(op.f(SHA), TABLE, ["domain_id", "sha256"], unique=True)
    op.drop_constraint(op.f("ck_documents_version"), TABLE, type_="check")
    op.drop_constraint(op.f("ck_documents_status"), TABLE, type_="check")
    for name in ("purged_by", "retired_by", "purged_at", "retired_at"):
        op.drop_column(TABLE, name)
    for name in ("status", "replaces", "version", "source_key"):
        op.drop_column(TABLE, name)
