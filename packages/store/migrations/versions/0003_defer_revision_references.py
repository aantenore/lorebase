"""Check the foreign keys that point at `revisions` at commit time.

Deleting a Domain cascades to its Revisions, parent edges and refs in one statement, in no
fixed order. `revision_parents.parent_id` and `refs.head` reference `revisions` without a
cascade (a Revision still referenced must not be deleted on its own), so an immediate check
could see an edge or a ref that the same cascade removes a moment later. Deferring the check to
the end of the transaction keeps the protection and makes the cascade order irrelevant.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINTS = (
    ("revision_parents", "fk_revision_parents_domain_id_parent_id_revisions"),
    ("refs", "fk_refs_domain_id_head_revisions"),
)


def upgrade() -> None:
    for table, name in _CONSTRAINTS:
        op.execute(f"ALTER TABLE {table} ALTER CONSTRAINT {name} DEFERRABLE INITIALLY DEFERRED")


def downgrade() -> None:
    for table, name in _CONSTRAINTS:
        op.execute(f"ALTER TABLE {table} ALTER CONSTRAINT {name} NOT DEFERRABLE")
