"""Agent trace session rows.

`agent_trace_sessions` holds the number of steps of each session and actor: the trace writer
raises it under the row lock in the transaction writing the steps, so the per-session cap holds
across Keepers. `agent_trace_session_domains` summarizes the steps of a session per Domain
(calls, notes, first and last step, tools, Proposals); sessions are listed from it instead of
grouping the steps of the retention period. Both are filled from the existing steps.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-27 11:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029"
down_revision: str | Sequence[str] | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SESSIONS = "agent_trace_sessions"
DOMAINS = "agent_trace_session_domains"

BYPASS = "current_setting('lorebase.bypass_rls', true) = 'on'"

DOMAIN_VISIBLE = (
    "current_setting('lorebase.bypass_rls', true) = 'on' "
    "OR domain_id = ANY (string_to_array(current_setting('lorebase.domain_ids', true), ','))"
)


def upgrade() -> None:
    op.create_table(
        SESSIONS,
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("steps", sa.Integer(), nullable=False),
        sa.Column("last_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("actor", "session_id", name=op.f("pk_agent_trace_sessions")),
    )
    op.create_table(
        DOMAINS,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("domain_id", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Integer(), nullable=False),
        sa.Column("inferred", sa.Boolean(), nullable=False),
        sa.Column("tools", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("proposals", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_trace_session_domains")),
        sa.UniqueConstraint(
            "actor",
            "session_id",
            "domain_id",
            name=op.f("uq_agent_trace_session_domains_actor_session_id_domain_id"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        op.f("ix_agent_trace_session_domains_actor_last_at"), DOMAINS, ["actor", "last_at"]
    )
    op.create_index(
        op.f("ix_agent_trace_session_domains_domain_id_last_at"), DOMAINS, ["domain_id", "last_at"]
    )
    # No Domain: readable and writable only with row-level security bypassed, as the trace
    # repository does.
    op.execute(f"ALTER TABLE {SESSIONS} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {SESSIONS} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY bypass_only ON {SESSIONS} USING ({BYPASS}) WITH CHECK ({BYPASS})")
    op.execute(f"ALTER TABLE {DOMAINS} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {DOMAINS} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY domain_isolation ON {DOMAINS} "
        f"USING ({DOMAIN_VISIBLE}) WITH CHECK ({DOMAIN_VISIBLE})"
    )
    op.execute("SELECT set_config('lorebase.bypass_rls', 'on', true)")
    op.execute(
        f"INSERT INTO {SESSIONS} (actor, session_id, steps, last_at) "
        "SELECT actor, session_id, count(*), max(at) FROM agent_trace_steps "
        "GROUP BY actor, session_id"
    )
    op.execute(
        f"INSERT INTO {DOMAINS} (actor, session_id, domain_id, started_at, last_at, calls, "
        "notes, inferred, tools, proposals) "
        "SELECT actor, session_id, domain_id, min(at), max(at), "
        "count(*) FILTER (WHERE type = 'call'), count(*) FILTER (WHERE type = 'note'), "
        "bool_or(inferred), "
        "array_remove(array_agg(DISTINCT tool), NULL), "
        "array_remove(array_agg(DISTINCT proposal_id), NULL) "
        "FROM agent_trace_steps GROUP BY actor, session_id, domain_id"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_trace_session_domains_domain_id_last_at"), table_name=DOMAINS)
    op.drop_index(op.f("ix_agent_trace_session_domains_actor_last_at"), table_name=DOMAINS)
    op.drop_table(DOMAINS)
    op.drop_table(SESSIONS)
