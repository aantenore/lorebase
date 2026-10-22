"""The plain reverse Link index again.

An earlier version of this revision replaced `ix_mat_links_reverse` with a covering index
(`INCLUDE (from_key, "values", ...)`) so that inbound Link hops of the neighborhood would be
index-only scans. It was withdrawn: a B-tree entry is limited to about 2.7 KB, so a Link with
large `values` could no longer be written ("index row size exceeds maximum"), and without
`values` the hop, which returns them, reads the heap anyway while the index doubles in size.
On a database that ran the withdrawn version this restores the plain index (built
concurrently per partition, revision 0011 style) and drops the covering one; elsewhere it
changes nothing.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-22 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PARENT = "mat_links"


class BtreeIndex(NamedTuple):
    name: str
    definition: str
    suffix: str
    """Partition indexes are named `<partition>_<suffix>`."""


OLD = BtreeIndex(
    "ix_mat_links_reverse",
    "(domain_id, branch, relation_id, to_key)",
    "domain_id_branch_relation_id_to_key_idx",
)
NEW = BtreeIndex(
    "ix_mat_links_reverse_covering",
    '(domain_id, branch, relation_id, to_key) INCLUDE (from_key, "values", object_hash, '
    "revision_generation, deleted)",
    "reverse_covering_idx",
)


def _partitions() -> list[str]:
    found = op.get_bind().execute(
        sa.text(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = CAST(:parent AS regclass) ORDER BY c.relname"
        ),
        {"parent": PARENT},
    )
    return list(found.scalars())


def _state(name: str) -> tuple[bool, bool] | None:
    """`(valid, attached)` of an index, `None` when it does not exist."""
    row = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT x.indisvalid, EXISTS (SELECT 1 FROM pg_inherits i "
                "WHERE i.inhrelid = c.oid) FROM pg_class c JOIN pg_index x "
                "ON x.indexrelid = c.oid WHERE c.relname = :name "
                "AND c.relnamespace = (SELECT relnamespace FROM pg_class "
                "WHERE oid = CAST(:parent AS regclass))"
            ),
            {"name": name, "parent": PARENT},
        )
        .first()
    )
    return None if row is None else (bool(row[0]), bool(row[1]))


def _build(index: BtreeIndex) -> None:
    """Build the index on every partition without blocking writes."""
    op.execute(f"CREATE INDEX IF NOT EXISTS {index.name} ON ONLY {PARENT} {index.definition}")
    with op.get_context().autocommit_block():
        for partition in _partitions():
            name = f"{partition}_{index.suffix}"
            state = _state(name)
            if state is not None and not state[0]:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
                state = None
            if state is None:
                op.execute(f"CREATE INDEX CONCURRENTLY {name} ON {partition} {index.definition}")
            if state is None or not state[1]:
                op.execute(f"ALTER INDEX {index.name} ATTACH PARTITION {name}")
    if _state(index.name) != (True, False):
        raise RuntimeError(f"index {index.name} is not valid after attaching every partition")


def _swap(old: BtreeIndex, new: BtreeIndex) -> None:
    """Build `new`, then drop `old` once `new` is valid."""
    _build(new)
    op.execute(f"DROP INDEX IF EXISTS {old.name}")


def upgrade() -> None:
    _swap(NEW, OLD)


def downgrade() -> None:
    """Nothing to undo: the plain index is the one revision 0025 had."""
