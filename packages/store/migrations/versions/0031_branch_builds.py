"""Branch builds, copy retries and schema reconciliation.

- `materialization` gains `building`: a branch whose rows a `store.materialize` job writes
  from the tree of its head in batches (a copy of an overlay or of an older Revision, an
  overlay detached from its base). Reads of its head and commits answer
  `branch-materializing` until the job is done.
- `branch_materializations.strategy`: `base` (copy the base rows an overlay reads, as before)
  or `tree` (write the rows from the tree of the head).
- `branch_materializations.failures`: failed jobs of the copy, for the retry backoff and the
  fallback to a tree build.
- `branch_materializations.reconcile`: the schema changes a branch got while it was copied
  (`{"refresh_kinds": [...]}`); the job recomputes derived values, path Links, titles and
  search vectors after the switch.
- The `phase` check admits the phases of a tree build and of the reconciliation.

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-27 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031"
down_revision: str | Sequence[str] | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "branch_materializations"
PHASES = (
    "records",
    "links",
    "history",
    "clear",
    "tree_history",
    "tree_rows",
    "switch",
    "prune",
    "derived",
    "paths",
    "search",
    "done",
)
OLD_PHASES = ("records", "links", "history", "switch", "done")


def _phase_check(phases: Sequence[str]) -> str:
    return "phase IN (" + ", ".join(f"'{p}'" for p in phases) + ")"


def upgrade() -> None:
    op.execute("ALTER TYPE materialization ADD VALUE IF NOT EXISTS 'building'")
    op.add_column(
        TABLE, sa.Column("strategy", sa.Text(), server_default=sa.text("'base'"), nullable=False)
    )
    op.add_column(
        TABLE, sa.Column("failures", sa.Integer(), server_default=sa.text("0"), nullable=False)
    )
    op.add_column(
        TABLE, sa.Column("reconcile", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_branch_materializations_strategy"), TABLE, "strategy IN ('base', 'tree')"
    )
    op.drop_constraint(op.f("ck_branch_materializations_phase"), TABLE, type_="check")
    op.create_check_constraint(
        op.f("ck_branch_materializations_phase"), TABLE, _phase_check(PHASES)
    )


def downgrade() -> None:
    # A branch still building has no materialized head to fall back to: rebuild it first.
    op.execute(
        "DELETE FROM branch_materializations WHERE phase NOT IN ("
        + ", ".join(f"'{p}'" for p in OLD_PHASES)
        + ")"
    )
    op.drop_constraint(op.f("ck_branch_materializations_phase"), TABLE, type_="check")
    op.create_check_constraint(
        op.f("ck_branch_materializations_phase"), TABLE, _phase_check(OLD_PHASES)
    )
    op.drop_constraint(op.f("ck_branch_materializations_strategy"), TABLE, type_="check")
    op.drop_column(TABLE, "reconcile")
    op.drop_column(TABLE, "failures")
    op.drop_column(TABLE, "strategy")
    # Postgres cannot drop an enum value; `building` stays unused.
