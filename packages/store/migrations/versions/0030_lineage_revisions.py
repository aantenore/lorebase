"""Revisions behind the lineage of materialized Records.

`mat_records.lineage_revisions` maps each Attribute of `lineage` to the Revision of its last
change and `$created` to the Revision that last created the Record, kept by the materializer
in the commit transaction. Rows written before this revision hold null and their readers
fall back to the branch history (`mat_history`); a rebuild fills them.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-27 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030"
down_revision: str | Sequence[str] | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mat_records",
        sa.Column("lineage_revisions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mat_records", "lineage_revisions")
