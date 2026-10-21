"""Client networks an API key may be used from (design 13.2).

`api_keys.allowed_networks` lists CIDR networks; Keeper refuses the key from any other client
address. Null, the value of every existing key, keeps a key usable from anywhere.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-21 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("allowed_networks", postgresql.ARRAY(postgresql.CIDR()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "allowed_networks")
