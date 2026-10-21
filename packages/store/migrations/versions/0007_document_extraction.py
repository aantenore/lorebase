"""Document blobs, extraction items and extraction job bookkeeping (design 7.3, 7.4, 9.2).

- `document_blobs` is the default blob store backend: document bytes in Postgres, addressed
  per Domain by their SHA-256 (`documents.blob_ref` is `postgres:<hex digest>`). Domain-scoped
  with row-level security like every table of 0001.
- `documents (domain_id, sha256)` becomes unique: uploading the same bytes again returns the
  stored document.
- `proposal_items` gains what an extraction item needs besides its decision: item type,
  subject (proposed Record, value or Link), match details, tier, model, job, reviewer edits
  and a stable order.
- `extraction_jobs` gains its author, timing, statistics, error and branch.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-21 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DOMAIN_VISIBLE = (
    "current_setting('lorebase.bypass_rls', true) = 'on' "
    "OR domain_id = ANY (string_to_array(current_setting('lorebase.domain_ids', true), ','))"
)
_JSON = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "document_blobs",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("sha256", sa.LargeBinary(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("octet_length(sha256) = 32", name=op.f("ck_document_blobs_sha256_h32")),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_document_blobs_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "sha256", name=op.f("pk_document_blobs")),
    )
    op.execute("ALTER TABLE document_blobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE document_blobs FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY domain_isolation ON document_blobs "
        f"USING ({DOMAIN_VISIBLE}) WITH CHECK ({DOMAIN_VISIBLE})"
    )

    op.drop_index(op.f("ix_documents_domain_id_sha256"), table_name="documents")
    op.create_index(
        op.f("ix_documents_domain_id_sha256"), "documents", ["domain_id", "sha256"], unique=True
    )

    op.add_column(
        "proposal_items",
        sa.Column("item_type", sa.Text(), server_default=sa.text("'record'"), nullable=False),
    )
    op.add_column(
        "proposal_items",
        sa.Column("seq", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("proposal_items", sa.Column("job_id", sa.Text(), nullable=True))
    op.add_column(
        "proposal_items",
        sa.Column("subject", _JSON, server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.add_column("proposal_items", sa.Column("match", _JSON, nullable=True))
    op.add_column("proposal_items", sa.Column("tier", sa.SmallInteger(), nullable=True))
    op.add_column("proposal_items", sa.Column("model", sa.Text(), nullable=True))
    op.add_column("proposal_items", sa.Column("edits", _JSON, nullable=True))
    op.add_column(
        "proposal_items",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_proposal_items_item_type"),
        "proposal_items",
        "item_type IN ('record', 'value', 'link')",
    )
    op.create_index(
        op.f("ix_proposal_items_domain_id_proposal_id_seq"),
        "proposal_items",
        ["domain_id", "proposal_id", "seq"],
        unique=False,
    )

    op.add_column("extraction_jobs", sa.Column("created_by", sa.Text(), nullable=True))
    op.add_column("extraction_jobs", sa.Column("branch", sa.Text(), nullable=True))
    op.add_column(
        "extraction_jobs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "extraction_jobs", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "extraction_jobs",
        sa.Column("stats", _JSON, server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.add_column("extraction_jobs", sa.Column("error", _JSON, nullable=True))
    op.create_index(
        op.f("ix_extraction_jobs_documents"),
        "extraction_jobs",
        ["documents"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_extraction_jobs_documents"), table_name="extraction_jobs")
    for column in ("error", "stats", "finished_at", "started_at", "branch", "created_by"):
        op.drop_column("extraction_jobs", column)
    op.drop_index(op.f("ix_proposal_items_domain_id_proposal_id_seq"), table_name="proposal_items")
    op.drop_constraint(op.f("ck_proposal_items_item_type"), "proposal_items", type_="check")
    for column in (
        "created_at",
        "edits",
        "model",
        "tier",
        "match",
        "subject",
        "job_id",
        "seq",
        "item_type",
    ):
        op.drop_column("proposal_items", column)
    op.drop_index(op.f("ix_documents_domain_id_sha256"), table_name="documents")
    op.create_index(
        op.f("ix_documents_domain_id_sha256"), "documents", ["domain_id", "sha256"], unique=False
    )
    op.drop_table("document_blobs")
