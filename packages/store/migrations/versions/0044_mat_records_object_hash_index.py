"""Index `mat_records` on `(domain_id, branch, object_hash)` for semantic search.

`embeddings` joins `mat_records` on `(domain_id, object_hash)` (design 8.8, filtered semantic
search). Without an index on that side of the join the planner cannot drive the HNSW scan lazily
under a nested loop: it hash-joins every vector of the model with the Records and sorts, a cost
linear in the table (82 ms at 100,000 vectors, 270 ms at 300,000). With it the HNSW index is read
and the Records are probed per candidate.

The index is built per partition with `CREATE INDEX CONCURRENTLY` (writes are not blocked),
attached to an index created `ON ONLY` the parent; an invalid leftover of an interrupted run is
rebuilt, as in revisions 0011 and 0026. Re-running changes nothing.

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-31 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | Sequence[str] | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PARENT = "mat_records"
INDEX = "ix_mat_records_object_hash"
DEFINITION = "(domain_id, branch, object_hash)"
SUFFIX = "domain_id_branch_object_hash_idx"
"""Partition indexes are named `<partition>_<suffix>`."""


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


def upgrade() -> None:
    op.execute(f"CREATE INDEX IF NOT EXISTS {INDEX} ON ONLY {PARENT} {DEFINITION}")
    with op.get_context().autocommit_block():
        for partition in _partitions():
            name = f"{partition}_{SUFFIX}"
            state = _state(name)
            if state is not None and not state[0]:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
                state = None
            if state is None:
                op.execute(f"CREATE INDEX CONCURRENTLY {name} ON {partition} {DEFINITION}")
            if state is None or not state[1]:
                op.execute(f"ALTER INDEX {INDEX} ATTACH PARTITION {name}")
    if _state(INDEX) != (True, False):
        raise RuntimeError(f"index {INDEX} is not valid after attaching every partition")


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
