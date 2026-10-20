"""Proposal and merge attempt details (design 6.10, 7.3).

- `proposals`: `idempotency_key` (unique per Domain and author when set) and `updated_at`;
- `merge_attempts`: `base` becomes nullable (no common ancestor, or a virtual base), plus
  `virtual_base`, `result`, `policy`, `created_by`, `outcome`, `revision` and
  `idempotency_key`.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-21 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ATTEMPT_COLUMNS = (
    "virtual_base",
    "result",
    "policy",
    "created_by",
    "outcome",
    "revision",
    "idempotency_key",
)


def upgrade() -> None:
    op.add_column("proposals", sa.Column("idempotency_key", sa.Text(), nullable=True))
    op.add_column("proposals", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "uq_proposals_idempotency",
        "proposals",
        ["domain_id", "author", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.alter_column("merge_attempts", "base", existing_type=sa.LargeBinary(), nullable=True)
    op.add_column(
        "merge_attempts",
        sa.Column("virtual_base", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("merge_attempts", sa.Column("result", sa.Text(), nullable=True))
    op.add_column(
        "merge_attempts",
        sa.Column("policy", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("merge_attempts", sa.Column("created_by", sa.Text(), nullable=True))
    op.add_column(
        "merge_attempts",
        sa.Column("outcome", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("merge_attempts", sa.Column("revision", sa.LargeBinary(), nullable=True))
    op.add_column("merge_attempts", sa.Column("idempotency_key", sa.Text(), nullable=True))


def downgrade() -> None:
    for column in reversed(_ATTEMPT_COLUMNS):
        op.drop_column("merge_attempts", column)
    op.execute("SET LOCAL lorebase.bypass_rls = 'on'")
    op.execute("DELETE FROM merge_attempts WHERE base IS NULL")
    op.alter_column("merge_attempts", "base", existing_type=sa.LargeBinary(), nullable=False)
    op.drop_index("uq_proposals_idempotency", table_name="proposals")
    op.drop_column("proposals", "updated_at")
    op.drop_column("proposals", "idempotency_key")
