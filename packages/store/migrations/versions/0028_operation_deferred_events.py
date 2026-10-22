"""Deferred Operation events.

`operation_deferred_events` holds the `operation.<side effect>` events of executions committed
on a branch whose Proposal is their review (agent writes). The merge that brings the execution
into its target writes them to the outbox and deletes them, in its transaction. Under the
`domain_isolation` policy like every table with a `domain_id`.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-27 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028"
down_revision: str | Sequence[str] | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFERRED = "operation_deferred_events"

DOMAIN_VISIBLE = (
    "current_setting('lorebase.bypass_rls', true) = 'on' "
    "OR domain_id = ANY (string_to_array(current_setting('lorebase.domain_ids', true), ','))"
)


def upgrade() -> None:
    op.create_table(
        DEFERRED,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("revision_id", sa.LargeBinary(), nullable=False),
        sa.Column("invocation_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_operation_deferred_events_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operation_deferred_events")),
    )
    op.create_index(
        op.f("ix_operation_deferred_events_domain_id_branch"), DEFERRED, ["domain_id", "branch"]
    )
    op.execute(f"ALTER TABLE {DEFERRED} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {DEFERRED} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY domain_isolation ON {DEFERRED} "
        f"USING ({DOMAIN_VISIBLE}) WITH CHECK ({DOMAIN_VISIBLE})"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_operation_deferred_events_domain_id_branch"), table_name=DEFERRED)
    op.drop_table(DEFERRED)
