"""Embedding jobs (design 9.3): text hashes of vectors and per-branch progress.

- `embeddings.text_hash`: SHA-256 of the text a vector was computed from. A Record whose text
  did not change keeps its vector, and an object whose text equals another's reuses that
  vector without a model call (index on `(domain_id, model, text_hash)`). Null for vectors
  written before this revision.
- `embedding_cursors`: per branch and model, the head and the template fingerprints of the
  last complete pass of the embedding job. Domain-scoped with row-level security like the
  tables of 0001; rows go with their branch.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-22 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DOMAIN_VISIBLE = (
    "current_setting('lorebase.bypass_rls', true) = 'on' "
    "OR domain_id = ANY (string_to_array(current_setting('lorebase.domain_ids', true), ','))"
)


def upgrade() -> None:
    op.add_column("embeddings", sa.Column("text_hash", sa.LargeBinary(), nullable=True))
    op.create_index(
        op.f("ix_embeddings_domain_id_model_text_hash"),
        "embeddings",
        ["domain_id", "model", "text_hash"],
        unique=False,
    )
    op.create_table(
        "embedding_cursors",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("head", sa.LargeBinary(), nullable=False),
        sa.Column("fingerprints", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id", "branch"],
            ["refs.domain_id", "refs.name"],
            name=op.f("fk_embedding_cursors_domain_id_branch_refs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "branch", "model", name=op.f("pk_embedding_cursors")),
    )
    op.execute("ALTER TABLE embedding_cursors ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE embedding_cursors FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY domain_isolation ON embedding_cursors "
        f"USING ({DOMAIN_VISIBLE}) WITH CHECK ({DOMAIN_VISIBLE})"
    )


def downgrade() -> None:
    op.drop_table("embedding_cursors")
    op.drop_index(op.f("ix_embeddings_domain_id_model_text_hash"), table_name="embeddings")
    op.drop_column("embeddings", "text_hash")
