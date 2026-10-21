"""One lookup per hop in the overlay neighborhood (store schema notes).

`lb_neighborhood_overlay` (revision 0010) looked Links and Records up by key through the
resolved rows of the overlay, `UNION ALL` of its rows and its base's: two index scans per
lookup, a primary-key probe of the overlay per base row found, and a larger statement to
plan for every hop and side. This revision adds `lb_overlay_lookup(domain, branch, table,
where)`, which renders a lookup of the resolved rows matching `where` as one index scan over
both branches (`branch IN (overlay, base)`, base rows up to the fork generation while the ref
is still that overlay), where the overlay's row of an identity, tombstone included, wins over
the base's (`DISTINCT ON`, overlay first); `lorebase_store.layout.BranchLayout.records_lookup`
renders the same text. The function's stored Link hops, foreign-key hops out of a Record and
final hydration use it; inbound foreign-key hops still read both halves through their
per-Attribute indexes. Signature, checks, owner and grants are those of revision 0010.

`where` is SQL text: only the read functions call the helper, with conditions they build
from constants; EXECUTE is revoked from PUBLIC.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-21 21:00:00.000000
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
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

HELPER = "lb_overlay_lookup(text, text, text, text)"
NEIGHBORHOOD = _overlays.SIGNATURES[2]

LB_OVERLAY_LOOKUP = f"""
CREATE FUNCTION lb_overlay_lookup(p_domain text, p_branch text, p_table text, p_where text)
RETURNS text
LANGUAGE plpgsql STABLE
SET search_path = {{search_path}}
AS $fn$
DECLARE
    v_columns text[];
    v_key text[];
    v_base text;
    v_generation bigint;
    v_select text;
    v_identity text;
BEGIN
    IF p_table = 'mat_records' THEN
        v_columns := {_overlays._array(_overlays._RECORD_COLUMNS)};
        v_key := ARRAY['kind_id', 'key'];
    ELSIF p_table = 'mat_links' THEN
        v_columns := {_overlays._array(_overlays._LINK_COLUMNS)};
        v_key := ARRAY['relation_id', 'from_key', 'to_key'];
    ELSE
        RAISE EXCEPTION 'unknown materialized table %', p_table USING ERRCODE = '22023';
    END IF;
    SELECT string_agg(
               CASE WHEN c = 'branch' THEN format('CAST(%L AS text) AS branch', p_branch)
                    ELSE 'x.' || quote_ident(c) END, ', ' ORDER BY n)
    INTO v_select FROM unnest(v_columns) WITH ORDINALITY AS t(c, n);
    SELECT r.base_branch, r.base_generation INTO v_base, v_generation
    FROM refs r
    WHERE r.domain_id = p_domain AND r.name = p_branch AND r.materialization = 'overlay';
    IF v_base IS NULL THEN
        -- Not (or no longer) an overlay: its own rows.
        RETURN format('(SELECT %s FROM %I x WHERE x.domain_id = %L AND x.branch = %L AND %s)',
                      v_select, p_table, p_domain, p_branch, p_where);
    END IF;
    SELECT string_agg('x.' || quote_ident(c), ', ' ORDER BY n) INTO v_identity
    FROM unnest(v_key) WITH ORDINALITY AS t(c, n);
    -- The rows of lorebase_store.layout.BranchLayout.records_lookup: the overlay's and the
    -- base's (up to the fork, while the ref is still that overlay) in one scan, the
    -- overlay's row of an identity first, tombstones dropped last.
    RETURN format(
        '(SELECT %1$s FROM (SELECT DISTINCT ON (%2$s) * FROM %3$I x '
        'WHERE x.domain_id = %4$L AND x.branch IN (%5$L, %6$L) '
        'AND (x.branch = %5$L OR (x.revision_generation <= %7$s '
        'AND EXISTS (SELECT 1 FROM refs f WHERE f.domain_id = %4$L AND f.name = %5$L '
        'AND f.base_branch = %6$L AND f.base_generation = %7$s OFFSET 0))) AND %8$s '
        'ORDER BY %2$s, x.branch = %5$L DESC) x WHERE NOT x.deleted)',
        v_select, v_identity, p_table, p_domain, p_branch, v_base, v_generation, p_where);
END
$fn$
"""


def _replaced(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"revision 0010 changed: {old!r} not found once")
    return text.replace(old, new)


_LINK_JOIN = """    v_join := format(
        'CROSS JOIN LATERAL (SELECT * FROM %s l WHERE l.domain_id = %L AND l.branch = %L '
        'AND l.relation_id = r.relation_id AND l.@near@ = w.key OFFSET 0) AS l',
        v_links, p_domain, p_branch);"""
_LINK_LOOKUP = """    v_join := 'CROSS JOIN LATERAL ' || lb_overlay_lookup(
        p_domain, p_branch, 'mat_links', 'x.relation_id = r.relation_id AND x.@near@ = w.key')
        || ' AS l';"""
_RECORD_JOIN = """'CAST(NULL AS bytea) FROM fk_rels f JOIN %s m ON m.domain_id = %L '
            'AND m.branch = %L AND m.kind_id = w.kind_id AND m.key = w.key '
            'WHERE f.from_kind = w.kind_id AND m."values" ->> f.attribute IS NOT NULL '
            'LIMIT $3', v_records, p_domain, p_branch);"""
_RECORD_LOOKUP = """'CAST(NULL AS bytea) FROM fk_rels f CROSS JOIN LATERAL %s m '
            'WHERE f.from_kind = w.kind_id AND m."values" ->> f.attribute IS NOT NULL '
            'LIMIT $3', lb_overlay_lookup(p_domain, p_branch, 'mat_records',
                                          'x.kind_id = w.kind_id AND x.key = w.key'));"""
_HYDRATE_JOIN = """    CROSS JOIN LATERAL (
        SELECT * FROM %5$s m WHERE m.domain_id = %3$L AND m.branch = %4$L
                               AND m.kind_id = n.kind_id AND m.key = n.key LIMIT 1) AS m"""
_HYDRATE_LOOKUP = "    CROSS JOIN LATERAL %6$s AS m"
_FORMAT_ARGUMENTS = "$sql$, v_nulls, v_step, p_domain, p_branch, v_records)"
_LOOKUP_ARGUMENTS = """$sql$, v_nulls, v_step, p_domain, p_branch, v_records,
        lb_overlay_lookup(p_domain, p_branch, 'mat_records',
                          'x.kind_id = n.kind_id AND x.key = n.key'))"""

LB_NEIGHBORHOOD_OVERLAY = _overlays.LB_NEIGHBORHOOD_OVERLAY
for _old, _new in (
    (_LINK_JOIN, _LINK_LOOKUP),
    (_RECORD_JOIN, _RECORD_LOOKUP),
    (_HYDRATE_JOIN, _HYDRATE_LOOKUP),
    (_FORMAT_ARGUMENTS, _LOOKUP_ARGUMENTS),
):
    LB_NEIGHBORHOOD_OVERLAY = _replaced(LB_NEIGHBORHOOD_OVERLAY, _old, _new)
"""Revision 0010's function with the lookups; inbound foreign-key hops still read
`v_records`, the resolved rows."""

_SOURCES = "v_records text;"
_RESOLVE = """
    v_records := coalesce(lb_overlay_rows(p_domain, p_branch, 'mat_records'), 'mat_records');
"""
"""Run after the checks of revision 0008, as in revision 0010; Links are read by lookup."""


def _neighborhood(template: str, sources: str, resolve: str) -> None:
    search_path = _reads._search_path()
    options = _reads._OPTIONS.format(search_path=search_path).strip()
    guards = _checks._guards(search_path, _overlays.COUNTS[_overlays.LB_NEIGHBORHOOD_OVERLAY])
    statement = _checks._create_or_replace(template, guards + resolve)
    op.execute(
        statement.format(options=options, record_columns=_reads._RECORD_COLUMNS, sources=sources)
    )
    # CREATE OR REPLACE keeps the owner and the grants; restate them all the same.
    op.execute(f"REVOKE ALL ON FUNCTION {NEIGHBORHOOD} FROM PUBLIC")
    role = _reads._application_role()
    if role is not None:
        quoted = op.get_bind().execute(sa.text("SELECT quote_ident(:r)"), {"r": role}).scalar()
        op.execute(f"GRANT EXECUTE ON FUNCTION {NEIGHBORHOOD} TO {quoted}")


def upgrade() -> None:
    role = _reads._application_role()
    if role is not None:
        _checks._refuse_writable_schemas(role)
    op.execute(LB_OVERLAY_LOOKUP.format(search_path=_reads._search_path()))
    op.execute(f"REVOKE ALL ON FUNCTION {HELPER} FROM PUBLIC")
    _neighborhood(LB_NEIGHBORHOOD_OVERLAY, _SOURCES, _RESOLVE)


def downgrade() -> None:
    _neighborhood(_overlays.LB_NEIGHBORHOOD_OVERLAY, _overlays._SOURCES.strip(), _overlays._RESOLVE)
    op.execute(f"DROP FUNCTION IF EXISTS {HELPER}")
