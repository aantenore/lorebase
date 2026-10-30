"""Value index limits held between passes (design 8.9).

Revision 0045 let the triggers add values of a selected Attribute until the next selection pass
noticed a branch holding too many and dropped it. This revision holds the limit at every write:

- `mat_value_attributes` gains `max_values` (the distinct values one branch may hold of the
  Attribute, set by the pass: `hard_max_distinct` for Attributes always held, `max_distinct`
  times `hysteresis` for plain strings) and `overflowed`: set once a branch holds more, after
  which the triggers stop counting the Attribute, readers skip it and the next pass drops it.
- `mat_value_sizes(domain_id, branch, kind_id, attribute_id, size)`: per branch, how many values
  of a selected Attribute `mat_values` holds. Rows go with their branch; row-level security as
  for revision 0045's tables.
- `lb_values_maintain` (same triggers) skips overflowed Attributes, and its `mat_values` upsert
  becomes one statement that also adds the values new to a branch less those spent (the
  inserted rows, and existing rows dropping to 0) to `mat_value_sizes`, in key order, and marks
  `overflowed` an Attribute a branch now holds more than `max_values` values of. An update
  between values a branch already holds writes no size.
- `lb_values_similar` reads only Attributes that are not overflowed.

Upgrade: existing selection rows get `hard_max_distinct` as `max_values` (no schema is read
here), their sizes are counted from `mat_values`, rows past it are marked, and the pass cursors
are cleared, so the next pass of each Domain decides again and sets the limit of every held
Attribute. Downgrade restores revision 0045's functions and drops the table and columns.

Revision ID: 0047
Revises: 0046
Create Date: 2026-11-03 10:00:00.000000
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic import op

revision: str = "0047"
down_revision: str | Sequence[str] | None = "0046"
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


_values = _revision_module("0045_value_index.py")
_checks = _values._checks
_reads = _values._reads

_ROWS = _values._ROWS.replace(
    "AND s.kind_id = {row}.kind_id\n",
    "AND s.kind_id = {row}.kind_id\n    AND NOT s.overflowed\n",
    1,
)
"""Revision 0045's rows of a statement, overflowed Attributes left out."""
if "NOT s.overflowed" not in _ROWS:
    raise RuntimeError("revision 0045's value rows changed")


def _rows(table: str, row: str, sign: int, *, live: bool, other: str | None) -> str:
    return _ROWS.format(
        row=row,
        table=table,
        sign=sign,
        live=_values._LIVE.format(row=row) if live else "",
        unchanged=_values._UNCHANGED.format(other=other, row=row) if other else "",
    ).strip()


INSERTED = _values._delta("lb_new", _rows("lb_new", "n", 1, live=False, other=None))
UPDATED = _values._delta(
    "lb_new",
    _rows("lb_new", "n", 1, live=False, other="lb_old"),
    _rows("lb_old", "o", -1, live=True, other="lb_new"),
)
DELETED = _values._delta("lb_old", _rows("lb_old", "o", -1, live=True, other=None))

_UPSERT = """\
    INSERT INTO mat_values AS m (domain_id, branch, kind_id, attribute_id, value, norm, records)
    SELECT d.domain_id, d.branch, d.kind_id, d.attribute_id, d.value, lb_value_norm(d.value),
           d.records
    FROM unnest(v_domains, v_branches, v_kind_ids, v_attributes, v_values, v_counts)
         WITH ORDINALITY AS d(domain_id, branch, kind_id, attribute_id, value, records, n)
    ORDER BY d.n
    ON CONFLICT (domain_id, branch, kind_id, attribute_id, value)
    DO UPDATE SET records = m.records + EXCLUDED.records;
"""
_SIZED_UPSERT = """\
    -- A value is new when its row is inserted (xmax 0) and spent when an existing row drops
    -- to 0 (rows at 0 never outlive the statement); the sizes take the difference, and an
    -- Attribute a branch now holds more than max_values values of is marked (module doc).
    WITH held AS (
        INSERT INTO mat_values AS m (domain_id, branch, kind_id, attribute_id, value, norm,
                                     records)
        SELECT d.domain_id, d.branch, d.kind_id, d.attribute_id, d.value, lb_value_norm(d.value),
               d.records
        FROM unnest(v_domains, v_branches, v_kind_ids, v_attributes, v_values, v_counts)
             WITH ORDINALITY AS d(domain_id, branch, kind_id, attribute_id, value, records, n)
        ORDER BY d.n
        ON CONFLICT (domain_id, branch, kind_id, attribute_id, value)
        DO UPDATE SET records = m.records + EXCLUDED.records
        RETURNING m.domain_id, m.branch, m.kind_id, m.attribute_id,
                  CASE WHEN m.xmax = 0 THEN CAST(m.records > 0 AS integer)
                       ELSE -CAST(m.records <= 0 AS integer) END AS grown
    ), sized AS (
        INSERT INTO mat_value_sizes AS z (domain_id, branch, kind_id, attribute_id, size)
        SELECT h.domain_id, h.branch, h.kind_id, h.attribute_id, sum(h.grown)
        FROM held h GROUP BY h.domain_id, h.branch, h.kind_id, h.attribute_id
        HAVING sum(h.grown) <> 0
        ORDER BY h.domain_id, h.branch, h.kind_id, h.attribute_id
        ON CONFLICT (domain_id, branch, kind_id, attribute_id)
        DO UPDATE SET size = z.size + EXCLUDED.size
        RETURNING z.domain_id, z.kind_id, z.attribute_id, z.size
    )
    UPDATE mat_value_attributes AS s SET overflowed = true
    FROM sized z
    WHERE s.domain_id = z.domain_id AND s.kind_id = z.kind_id
      AND s.attribute_id = z.attribute_id AND z.size > s.max_values AND NOT s.overflowed;
"""


def _maintain(template: str) -> str:
    """Revision 0045's trigger function as `CREATE OR REPLACE`."""
    return template.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


def _held_maintain() -> str:
    statement = _values.VALUES_MAINTAIN
    for old, new in (
        (_values.INSERTED, INSERTED),
        (_values.UPDATED, UPDATED),
        (_values.DELETED, DELETED),
        (_UPSERT, _SIZED_UPSERT),
    ):
        if statement.count(old) != 1:
            raise RuntimeError("revision 0045's lb_values_maintain changed")
        statement = statement.replace(old, new, 1)
    return _maintain(statement)


VALUES_MAINTAIN = _held_maintain()

_SELECTED = "'AND s.attribute_id = v.attribute_id '\n        'WHERE"
_BASE_SELECTED = (
    "'AND s.kind_id = v.kind_id AND s.attribute_id = v.attribute_id '\n            'WHERE"
)
_WITHIN = (
    "'AND NOT s.overflowed AND NOT EXISTS (SELECT 1 FROM mat_value_sizes z '\n"
    "{indent}'WHERE z.domain_id = v.domain_id AND z.branch = v.branch '\n"
    "{indent}'AND z.kind_id = v.kind_id AND z.attribute_id = v.attribute_id '\n"
    "{indent}'AND z.size > s.max_values) '\n{indent}'WHERE"
)
"""Values of an Attribute some read branch holds more values of than its limit are never
returned, marked or not (a commit lowering the limit races with writers of other branches)."""
LB_VALUES_SIMILAR = _values.LB_VALUES_SIMILAR.replace(
    _SELECTED,
    "'AND s.attribute_id = v.attribute_id '\n        " + _WITHIN.format(indent=" " * 8),
    1,
).replace(
    _BASE_SELECTED,
    "'AND s.kind_id = v.kind_id AND s.attribute_id = v.attribute_id '\n            "
    + _WITHIN.format(indent=" " * 12),
    1,
)
if LB_VALUES_SIMILAR.count("NOT s.overflowed") != 2:
    raise RuntimeError("revision 0045's lb_values_similar changed")

DOMAIN_VISIBLE = _values.DOMAIN_VISIBLE

_BYPASS = "SELECT set_config('lorebase.bypass_rls', 'on', true)"
"""Row-level security is forced on the owner too: read and write the rows of every Domain."""
_LIMITS = "UPDATE mat_value_attributes SET max_values = :limit"
_CLEAR_SIZES = "DELETE FROM mat_value_sizes"
_COUNT_SIZES = """
INSERT INTO mat_value_sizes (domain_id, branch, kind_id, attribute_id, size)
SELECT v.domain_id, v.branch, v.kind_id, v.attribute_id, count(*) FROM mat_values v
JOIN mat_value_attributes s ON s.domain_id = v.domain_id AND s.kind_id = v.kind_id
    AND s.attribute_id = v.attribute_id
GROUP BY v.domain_id, v.branch, v.kind_id, v.attribute_id
"""
_MARK = """
UPDATE mat_value_attributes s SET overflowed = true WHERE EXISTS (
    SELECT 1 FROM mat_value_sizes z WHERE z.domain_id = s.domain_id AND z.kind_id = s.kind_id
    AND z.attribute_id = s.attribute_id AND z.size > s.max_values)
"""
_CLEAR_CURSORS = "DELETE FROM mat_value_cursors"
_CLEAR_CALLERS = "UPDATE resolve_misses SET callers = '{}'::text[] WHERE callers <> '{}'::text[]"
"""Caller digests stored before keyed digests (plain BLAKE2b of the principal) go; phrases and
counts stay."""


def _data(bind: sa.Connection, limit: int) -> None:
    """Every row this revision rewrites, across Domains: limits, sizes and marks of the held
    Attributes, pass cursors (the next pass decides again) and unkeyed caller digests."""
    bind.execute(sa.text(_BYPASS))
    bind.execute(sa.text(_LIMITS), {"limit": limit})
    for statement in (_CLEAR_SIZES, _COUNT_SIZES, _MARK, _CLEAR_CURSORS, _CLEAR_CALLERS):
        bind.execute(sa.text(statement))


def _similar(template: str, search_path: str) -> None:
    options = _reads._OPTIONS.format(search_path=search_path).strip()
    guards = _checks._guards(search_path, ("p_limit",))
    statement = _checks._create_or_replace(template, guards)
    op.execute(statement.format(options=options, max_terms=_values.MAX_TERMS))
    _values._lexicon._grant((_values.SIMILAR,))


def upgrade() -> None:
    role = _reads._application_role()
    if role is not None:
        _checks._refuse_writable_schemas(role)
    search_path = _reads._search_path()
    limit = int(op.get_context().config.attributes["store_settings"].values.hard_max_distinct)
    op.add_column("mat_value_attributes", sa.Column("max_values", sa.Integer(), nullable=True))
    op.add_column(
        "mat_value_attributes",
        sa.Column("overflowed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_table(
        "mat_value_sizes",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("kind_id", sa.Text(), nullable=False),
        sa.Column("attribute_id", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id", "branch"],
            ["refs.domain_id", "refs.name"],
            name=op.f("fk_mat_value_sizes_domain_id_branch_refs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "domain_id", "branch", "kind_id", "attribute_id", name=op.f("pk_mat_value_sizes")
        ),
    )
    _values._isolate("mat_value_sizes")
    _data(op.get_bind(), limit)
    op.alter_column("mat_value_attributes", "max_values", nullable=False)
    op.execute(VALUES_MAINTAIN.format(search_path=search_path))
    _similar(LB_VALUES_SIMILAR, search_path)


def downgrade() -> None:
    search_path = _reads._search_path()
    _similar(_values.LB_VALUES_SIMILAR, search_path)
    op.execute(_maintain(_values.VALUES_MAINTAIN).format(search_path=search_path))
    op.drop_table("mat_value_sizes")
    op.drop_column("mat_value_attributes", "overflowed")
    op.drop_column("mat_value_attributes", "max_values")
