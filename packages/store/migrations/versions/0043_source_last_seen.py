"""When a Source last saw each element (design 9.1 "Incremental Sources").

`source_written.last_seen_run` and `last_seen_at` name the run of an `incremental` Source that
last read the element upstream, changed or not, and when. The reconciliation sweep raises a
`stale` candidate (`source_unseen`) for Records no run has seen for longer than the Source's
`stale_after`. Both are null on existing rows; readers fall back to `updated_at` (the last
state advance) until the next run sees the element. Additive: no data is rewritten.

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-31 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | Sequence[str] | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "source_written"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("last_seen_run", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column(TABLE, "last_seen_at")
    op.drop_column(TABLE, "last_seen_run")
