"""Open Conflicts by element (design 6.10).

A read of a Record can ask whether a Conflict about it waits for a decision in an open
Proposal. `conflicts` was only reachable by attempt, so that question would scan every
Conflict of every conflicted attempt. The partial index `ix_conflicts_open_element` over
`(domain_id, partition, element_key)` holds the unresolved Conflicts only: resolved rows, the
bulk of a table that keeps merged attempts, are left out, and the lookup of one Record is one
probe that finds nothing in the common case.

Revision ID: 0048
Revises: 0047
Create Date: 2026-11-13 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | Sequence[str] | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "conflicts"
INDEX = "ix_conflicts_open_element"


def upgrade() -> None:
    op.create_index(
        INDEX,
        TABLE,
        ["domain_id", "partition", "element_key"],
        postgresql_where=sa.text("resolved_by IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE)
