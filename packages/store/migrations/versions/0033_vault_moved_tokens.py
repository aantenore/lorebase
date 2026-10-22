"""Vault rows remember the key change that took their value over (design 13.5).

`vault.moved_to` is the Record key a key change stored the row's value again under. A moved
token can be taken over by no other key change and claimed again by no Record, so one value is
never copied to more than one new key, whichever branch the later attempt is made on.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-28 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | Sequence[str] | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("vault", sa.Column("moved_to", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("vault", "moved_to")
