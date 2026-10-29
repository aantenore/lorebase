"""Last-written state of Sources (design 9.1 "Corrections that stick").

`source_written` holds, per Source, target branch, Kind or Relation and key, the hashes of the
values the Source last wrote and the Source branch Revision that holds them;
`source_written_pending` the state a run carries in its open Proposal, moved to
`source_written` when the Proposal merges. Both are empty at first: Sources seed their state
on their next run. Under the `domain_isolation` policy like every table with a `domain_id`.

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-29 20:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040"
down_revision: str | Sequence[str] | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WRITTEN = "source_written"
PENDING = "source_written_pending"

DOMAIN_VISIBLE = (
    "current_setting('lorebase.bypass_rls', true) = 'on' "
    "OR domain_id = ANY (string_to_array(current_setting('lorebase.domain_ids', true), ','))"
)


def _state_columns() -> list[sa.Column[object]]:
    return [
        sa.Column(
            "written",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("revision_id", sa.LargeBinary(), nullable=True),
        sa.Column(
            "value_revisions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    ]


def _tail_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def _isolate(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY domain_isolation ON {table} "
        f"USING ({DOMAIN_VISIBLE}) WITH CHECK ({DOMAIN_VISIBLE})"
    )


def upgrade() -> None:
    op.create_table(
        WRITTEN,
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("target_branch", sa.Text(), nullable=False),
        sa.Column("element", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        *_state_columns(),
        *_tail_columns(),
        sa.CheckConstraint(
            "octet_length(revision_id) = 32", name=op.f("ck_source_written_revision_id_h32")
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_source_written_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "domain_id",
            "source_id",
            "target_branch",
            "element",
            "key",
            name=op.f("pk_source_written"),
        ),
    )
    op.create_table(
        PENDING,
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Text(), nullable=False),
        sa.Column("element", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("target_branch", sa.Text(), nullable=False),
        *_state_columns(),
        sa.Column(
            "preserve",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        *_tail_columns(),
        sa.CheckConstraint(
            "octet_length(revision_id) = 32",
            name=op.f("ck_source_written_pending_revision_id_h32"),
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_source_written_pending_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "domain_id", "proposal_id", "element", "key", name=op.f("pk_source_written_pending")
        ),
    )
    op.create_index(
        op.f("ix_source_written_pending_domain_id_source_id_target_branch"),
        PENDING,
        ["domain_id", "source_id", "target_branch"],
    )
    _isolate(WRITTEN)
    _isolate(PENDING)


def downgrade() -> None:
    op.drop_index(
        op.f("ix_source_written_pending_domain_id_source_id_target_branch"), table_name=PENDING
    )
    op.drop_table(PENDING)
    op.drop_table(WRITTEN)
