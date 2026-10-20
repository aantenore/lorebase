"""Webhooks and their deliveries (design 10.5).

Both tables are Domain-scoped: row-level security on `domain_id` like every such table of
0001. The delivery worker reads across Domains with `lorebase.bypass_rls`.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DOMAIN_SCOPED = ("webhooks", "webhook_deliveries")
DOMAIN_VISIBLE = (
    "current_setting('lorebase.bypass_rls', true) = 'on' "
    "OR domain_id = ANY (string_to_array(current_setting('lorebase.domain_ids', true), ','))"
)


def upgrade() -> None:
    op.create_table(
        "webhooks",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("event_types", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("branches", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("secret_hash", sa.LargeBinary(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(secret_hash) = 32", name=op.f("ck_webhooks_secret_hash_h32")
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_webhooks_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "id", name=op.f("pk_webhooks")),
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("webhook_id", sa.Text(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'dead')",
            name=op.f("ck_webhook_deliveries_status"),
        ),
        sa.ForeignKeyConstraint(
            ["domain_id", "webhook_id"],
            ["webhooks.domain_id", "webhooks.id"],
            name=op.f("fk_webhook_deliveries_domain_id_webhook_id_webhooks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "id", name=op.f("pk_webhook_deliveries")),
        sa.UniqueConstraint(
            "domain_id",
            "webhook_id",
            "event_id",
            name=op.f("uq_webhook_deliveries_domain_id_webhook_id_event_id"),
        ),
    )
    op.create_index(
        "ix_webhook_deliveries_due",
        "webhook_deliveries",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    for table in DOMAIN_SCOPED:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY domain_isolation ON {table} "
            f"USING ({DOMAIN_VISIBLE}) WITH CHECK ({DOMAIN_VISIBLE})"
        )


def downgrade() -> None:
    # Indexes and policies go with their tables.
    op.drop_table("webhook_deliveries")
    op.drop_table("webhooks")
