"""The Kinds a Proposal touches (design 10.7, stewards).

`proposals.kinds` lists the ids of the Kinds whose Records or Links (either end) the
Proposal's changes touch, refreshed with each merge attempt, so the steward roles of a
Proposal are an index lookup. Existing rows read null ("not computed yet") until their next
merge preview; a GIN index serves the containment test.

Revision ID: 0042
Revises: 0041
Create Date: 2026-10-31 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042"
down_revision: str | Sequence[str] | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "proposals"
INDEX = "ix_proposals_kinds"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("kinds", postgresql.ARRAY(sa.Text()), nullable=True))
    op.create_index(INDEX, TABLE, ["kinds"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_column(TABLE, "kinds")
