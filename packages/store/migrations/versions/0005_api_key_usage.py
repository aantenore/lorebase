"""Daily usage of API keys, for per-key quotas (design 13.6).

One row per key and UTC day holds the requests served and the context-pack tokens delivered.
The table is installation-wide like `api_keys`; rows go with their key.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-21 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_key_usage",
        sa.Column("api_key_id", sa.Text(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("requests", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("context_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_keys.id"],
            name=op.f("fk_api_key_usage_api_key_id_api_keys"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("api_key_id", "day", name=op.f("pk_api_key_usage")),
    )


def downgrade() -> None:
    op.drop_table("api_key_usage")
