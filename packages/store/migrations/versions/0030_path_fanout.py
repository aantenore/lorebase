"""The fan-out of materialized path Derivations.

`mat_schema.path_fanout` records the `store.traverse.path_fanout` a branch's materialized
path Derivations were computed with. A commit, a rebuild or the job worker that finds another
setting rewrites them (`lorebase_store.materialize.paths`). Existing rows start without one, so
their materialized paths are rewritten once.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-27 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | Sequence[str] | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mat_schema", sa.Column("path_fanout", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("mat_schema", "path_fanout")
