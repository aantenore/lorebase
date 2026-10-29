"""Indexes of reconciliation candidate lookups (design 10.6).

`(domain_id, proposal_id)` serves the `reconciliation` Proposal check and the settlement of a
merged Proposal; a GIN index (`jsonb_path_ops`) on `keys` serves the containment test of the
Record page (`keys @> '["key"]'`), which the btree index on `(domain_id, branch, kind_id,
keys)` cannot answer.

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-29 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0039"
down_revision: str | Sequence[str] | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CANDIDATES = "reconciliation_candidates"
BY_PROPOSAL = "ix_reconciliation_candidates_domain_id_proposal_id"
BY_KEY = "ix_reconciliation_candidates_keys"


def upgrade() -> None:
    op.create_index(BY_PROPOSAL, CANDIDATES, ["domain_id", "proposal_id"])
    op.create_index(
        BY_KEY,
        CANDIDATES,
        ["keys"],
        postgresql_using="gin",
        postgresql_ops={"keys": "jsonb_path_ops"},
    )


def downgrade() -> None:
    op.drop_index(BY_KEY, table_name=CANDIDATES)
    op.drop_index(BY_PROPOSAL, table_name=CANDIDATES)
