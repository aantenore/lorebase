"""Snapshot refs.

Adds `snapshot` to `ref_kind`: a snapshot ref holds the materialized rows of a past Revision
for reads at it (`lorebase_store.materialize.snapshot`). The value is added outside the
migration transaction, as Postgres requires before it can be used.

Downgrade deletes the snapshot refs (their rows go with them) and keeps the enum value:
Postgres cannot drop a value from an enum type, and no other ref uses it.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-27 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | Sequence[str] | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE ref_kind ADD VALUE IF NOT EXISTS 'snapshot'")


def downgrade() -> None:
    op.execute("DELETE FROM refs WHERE kind = 'snapshot'")
