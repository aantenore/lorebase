"""Inbound Link hops read from the index alone (design 8.7; store schema notes).

The neighborhood walk (`lb_neighborhood`, `lb_neighborhood_overlay`) follows inbound Links
through `ix_mat_links_reverse` on `(domain_id, branch, relation_id, to_key)` and fetched every
Link it found from the heap for `from_key`, `values` and `object_hash`. The Links pointing at a
Record were written by the commits of the Records they come from, so they lie on as many heap
pages as there are Links: about one page read per Link walked. This revision replaces it with
`ix_mat_links_reverse_covering`, the same key with `INCLUDE (from_key, "values", object_hash,
revision_generation, deleted)`, so the hop is an index-only scan on full branches and on both
halves of an overlay (its lookup filters on `revision_generation` and `deleted`): the Links of
one target are adjacent in the index. Pages not yet marked all-visible by vacuum are still
read from the heap.

Index keys and the included columns are kept without deduplication, so the index is about
twice the size of the one it replaces. The swap follows revision 0011: the parent index is
created `ON ONLY mat_links`, each partition's index is built concurrently and attached, and
the old index is dropped once the new one is valid.

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
    _swap(OLD, NEW)


def downgrade() -> None:
    _swap(NEW, OLD)
