"""Background branch copies and deletions (store schema notes, "Large branches").

- `refs.deleted_at`: set on a branch whose deletion is in progress. The ref is hidden from
  every read and write, its rows are deleted in batches by a `store.branch_delete` job, and
  the job deletes the ref last (its foreign keys cascade what is left).
- `branch_materializations`: per branch, the progress of a `store.materialize` job that turns
  an overlay into a full copy in batches (status, phase, cursor, rows done and total, error).
  Domain-scoped with row-level security like the tables of 0001; rows go with their branch.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-22 21:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DOMAIN_VISIBLE = (
    "current_setting('lorebase.bypass_rls', true) = 'on' "
    "OR domain_id = ANY (string_to_array(current_setting('lorebase.domain_ids', true), ','))"
)


def upgrade() -> None:
    op.add_column("refs", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "branch_materializations",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("base_branch", sa.Text(), nullable=False),
        sa.Column("base_generation", sa.BigInteger(), nullable=False),
        sa.Column("cursor", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("rows_done", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("rows_total", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed')",
            name=op.f("ck_branch_materializations_status"),
        ),
        sa.CheckConstraint(
            "phase IN ('records', 'links', 'history', 'switch', 'done')",
            name=op.f("ck_branch_materializations_phase"),
        ),
        sa.ForeignKeyConstraint(
            ["domain_id", "branch"],
            ["refs.domain_id", "refs.name"],
            name=op.f("fk_branch_materializations_domain_id_branch_refs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "branch", name=op.f("pk_branch_materializations")),
    )
    op.execute("ALTER TABLE branch_materializations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE branch_materializations FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY domain_isolation ON branch_materializations "
        f"USING ({DOMAIN_VISIBLE}) WITH CHECK ({DOMAIN_VISIBLE})"
    )


def downgrade() -> None:
    op.drop_table("branch_materializations")
    op.drop_column("refs", "deleted_at")
