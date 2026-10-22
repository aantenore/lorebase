"""Agent trace steps.

`agent_trace_steps` holds the steps of traced agent sessions (`keeper.trace`): one row per
traced request and per note an agent attached to its session. Debugging data, written best
effort and pruned after the retention period; under the `domain_isolation` policy like every
table with a `domain_id`.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-26 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STEPS = "agent_trace_steps"

DOMAIN_VISIBLE = (
    "current_setting('lorebase.bypass_rls', true) = 'on' "
    "OR domain_id = ANY (string_to_array(current_setting('lorebase.domain_ids', true), ','))"
)


def _json(name: str) -> sa.Column[object]:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), nullable=True)


def upgrade() -> None:
    op.create_table(
        STEPS,
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("principal_type", sa.Text(), nullable=True),
        sa.Column("domain_id", sa.Text(), nullable=True),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=True),
        sa.Column("method", sa.Text(), nullable=True),
        sa.Column("route", sa.Text(), nullable=True),
        sa.Column("status", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Double(), nullable=True),
        sa.Column("cache", sa.Text(), nullable=True),
        sa.Column("branch", sa.Text(), nullable=True),
        _json("query"),
        _json("result"),
        _json("refs"),
        _json("explain"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("proposal_id", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("inferred", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=True),
        sa.CheckConstraint("type IN ('call', 'note')", name=op.f("ck_agent_trace_steps_type")),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_agent_trace_steps")),
    )
    op.create_index(
        op.f("ix_agent_trace_steps_actor_session_id_seq"), STEPS, ["actor", "session_id", "seq"]
    )
    op.create_index(op.f("ix_agent_trace_steps_session_id_seq"), STEPS, ["session_id", "seq"])
    op.create_index(op.f("ix_agent_trace_steps_domain_id_at"), STEPS, ["domain_id", "at"])
    op.create_index(op.f("ix_agent_trace_steps_at"), STEPS, ["at"])
    op.create_index(
        op.f("ix_agent_trace_steps_actor_fingerprint_at"),
        STEPS,
        ["actor", "fingerprint", "at"],
        postgresql_where=sa.text("inferred"),
    )
    op.execute(f"ALTER TABLE {STEPS} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {STEPS} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY domain_isolation ON {STEPS} "
        f"USING ({DOMAIN_VISIBLE}) WITH CHECK ({DOMAIN_VISIBLE})"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_trace_steps_actor_fingerprint_at"), table_name=STEPS)
    op.drop_index(op.f("ix_agent_trace_steps_at"), table_name=STEPS)
    op.drop_index(op.f("ix_agent_trace_steps_domain_id_at"), table_name=STEPS)
    op.drop_index(op.f("ix_agent_trace_steps_session_id_seq"), table_name=STEPS)
    op.drop_index(op.f("ix_agent_trace_steps_actor_session_id_seq"), table_name=STEPS)
    op.drop_table(STEPS)
