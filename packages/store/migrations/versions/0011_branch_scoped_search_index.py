"""Search indexes scoped by Domain and branch (design 8.7; store schema notes).

The GIN indexes of revision 0001 on `search` (`ix_mat_records_search`) and on `title`
(trigrams, `ix_mat_records_title`) index that column alone, so each is shared by every Domain
and branch of a hash partition: a search reads the matches of all of them and keeps its own
when it rechecks the rows, and a search on an overlay branch does so once per half. This
revision replaces them with `ix_mat_records_branch_search` on `(domain_id, branch, search)`
and `ix_mat_records_branch_title` on `(domain_id, branch, title gin_trgm_ops)`; the
`btree_gin` extension (contrib) provides the GIN operator classes for the two text columns,
so a search's Domain and branch are index conditions and the bitmap holds the branch's
matches only. The extension is created if missing and never dropped on downgrade, like the
extensions of revision 0001.

Building and swapping each index does not block writes for long:

- `CREATE INDEX ... ON ONLY mat_records` makes the parent index (invalid, no partition yet);
- outside the migration transaction, each partition gets its index with `CREATE INDEX
  CONCURRENTLY` (an invalid leftover of an interrupted build is dropped and built again) and
  is attached to the parent, which becomes valid when every partition is attached;
- the old index is dropped only once the new one is valid; dropping a partitioned index takes
  a short exclusive lock on `mat_records` (catalog work, no scan).

Search on an overlay also stops probing the overlay's primary key once per base match: the
anti-join of `lb_overlay_rows` gets the argument `p_probe` (default true: `OFFSET 0` keeps it a
probe per base row, right for lookups by key and short pages); `lb_search_overlay` passes
false and lets the planner hash the overlay's keys when the matches outnumber them. Its
signature, checks, owner and grants stay those of revision 0010.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-21 20:00:00.000000
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _revision_module(filename: str) -> ModuleType:
    """An earlier revision file of this directory, loaded for its SQL templates and checks."""
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(f"_lorebase_revision_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load migration {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_overlays = _revision_module("0010_overlay_branches.py")
_checks = _overlays._checks
_reads = _overlays._reads

PARENT = "mat_records"


class GinIndex(NamedTuple):
    name: str
    columns: str
    suffix: str
    """Partition indexes are named `<partition>_<suffix>`, as Postgres names those it creates
    for an index on the parent."""


SWAPS = (
    (
        GinIndex("ix_mat_records_search", "search", "search_idx"),
        GinIndex(
            "ix_mat_records_branch_search",
            "domain_id, branch, search",
            "domain_id_branch_search_idx",
        ),
    ),
    (
        GinIndex("ix_mat_records_title", "title gin_trgm_ops", "title_idx"),
        GinIndex(
            "ix_mat_records_branch_title",
            "domain_id, branch, title gin_trgm_ops",
            "domain_id_branch_title_idx",
        ),
    ),
)
"""(index of revision 0001, its branch-scoped replacement)."""

OLD_HELPER = "lb_overlay_rows(text, text, text, text)"
HELPER = "lb_overlay_rows(text, text, text, text, boolean)"
SEARCH = _overlays.SIGNATURES[0]


def _replaced(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"revision 0010 changed: {old!r} not found once")
    return text.replace(old, new)


LB_OVERLAY_ROWS = _replaced(
    _replaced(
        _replaced(
            _overlays.LB_OVERLAY_ROWS,
            "p_tail text DEFAULT NULL)",
            "p_tail text DEFAULT NULL,\n    p_probe boolean DEFAULT true)",
        ),
        "AND %8$s OFFSET 0)',",
        "AND %8$s%9$s)',",
    ),
    "v_from_base, p_table, p_domain, v_base, v_generation, p_branch, v_s, v_shadow);",
    "v_from_base, p_table, p_domain, v_base, v_generation, p_branch, v_s, v_shadow,\n"
    "        CASE WHEN p_probe THEN ' OFFSET 0' ELSE '' END);",
)
"""Revision 0010's helper with `p_probe`: false renders the anti-join without `OFFSET 0`."""

_SEARCH_SOURCES = "v_records text;"
_SEARCH_RESOLVE = """
    v_records := coalesce(
        lb_overlay_rows(p_domain, p_branch, 'mat_records', NULL, false), 'mat_records');
"""
"""Run after the checks of revision 0008, as in revision 0010; search reads no Links."""


def _overlay_search(sources: str, resolve: str) -> None:
    search_path = _reads._search_path()
    options = _reads._OPTIONS.format(search_path=search_path).strip()
    counts = _overlays.COUNTS[_overlays.LB_SEARCH_OVERLAY]
    guards = _checks._guards(search_path, counts) + resolve
    statement = _checks._create_or_replace(_overlays.LB_SEARCH_OVERLAY, guards)
    op.execute(
        statement.format(options=options, record_columns=_reads._RECORD_COLUMNS, sources=sources)
    )
    # CREATE OR REPLACE keeps the owner and the grants; restate them all the same.
    op.execute(f"REVOKE ALL ON FUNCTION {SEARCH} FROM PUBLIC")
    role = _reads._application_role()
    if role is not None:
        quoted = op.get_bind().execute(sa.text("SELECT quote_ident(:r)"), {"r": role}).scalar()
        op.execute(f"GRANT EXECUTE ON FUNCTION {SEARCH} TO {quoted}")


def _helper(drop: str, create: str, signature: str) -> None:
    """Swap the helper; the read functions resolve it by name when they run."""
    op.execute(f"DROP FUNCTION IF EXISTS {drop}")
    op.execute(create.format(search_path=_reads._search_path()))
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")


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


def _build(index: GinIndex) -> None:
    """Build the GIN index on every partition without blocking writes."""
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {index.name} ON ONLY {PARENT} USING gin ({index.columns})"
    )
    with op.get_context().autocommit_block():
        for partition in _partitions():
            name = f"{partition}_{index.suffix}"
            state = _state(name)
            if state is not None and not state[0]:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
                state = None
            if state is None:
                op.execute(
                    f"CREATE INDEX CONCURRENTLY {name} ON {partition} USING gin ({index.columns})"
                )
            if state is None or not state[1]:
                op.execute(f"ALTER INDEX {index.name} ATTACH PARTITION {name}")
    if _state(index.name) != (True, False):
        raise RuntimeError(f"index {index.name} is not valid after attaching every partition")


def _swap(old: GinIndex, new: GinIndex) -> None:
    """Build `new`, then drop `old` once `new` is valid."""
    _build(new)
    op.execute(f"DROP INDEX IF EXISTS {old.name}")


def upgrade() -> None:
    role = _reads._application_role()
    if role is not None:
        _checks._refuse_writable_schemas(role)
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gin")
    _helper(OLD_HELPER, LB_OVERLAY_ROWS, HELPER)
    _overlay_search(_SEARCH_SOURCES, _SEARCH_RESOLVE)
    for old, new in SWAPS:
        _swap(old, new)


def downgrade() -> None:
    for old, new in reversed(SWAPS):
        _swap(new, old)
    _helper(HELPER, _overlays.LB_OVERLAY_ROWS, OLD_HELPER)
    _overlay_search(_overlays._SOURCES.strip(), _overlays._RESOLVE)
