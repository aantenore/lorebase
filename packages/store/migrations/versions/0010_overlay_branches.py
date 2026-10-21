"""Overlay branches (design 6.11, AC-16; store schema notes).

An overlay branch stores only the materialized rows it changed and reads the rows of its base
branch as they were when it was forked:

- `refs.base_branch`, `refs.base_revision` and `refs.base_generation` name the base and the
  fork point; they are set exactly when `materialization = 'overlay'`. Both references are
  checked at commit time, like `refs.head` (revision `0004`), so deleting a Domain cascades in
  any order; a base branch cannot be deleted while an overlay depends on it.
- `mat_records.revision_generation` and `mat_links.revision_generation` hold the generation of
  the Revision that last wrote the row (`0` for rows written before this revision, when no
  overlay existed); `deleted` marks the tombstones of rows an overlay removed.
- `lb_overlay_rows(domain, branch, table)` renders the resolved rows of an overlay as SQL
  text (`lorebase_store.layout` renders the same text in Python), and the read functions of
  revision `0005` get overlay variants (`lb_search_overlay`, `lb_links_in_overlay`,
  `lb_neighborhood_overlay`, `lb_query_by_attribute_overlay`) that read through it. They
  have the signatures, options and grants of the originals and the argument, scope and
  search_path checks revision `0008` added to them (counts bounded by
  `store.reads.definer_ceilings` as read when this revision runs); on a branch that is not (or
  no longer) an overlay they read its own rows, so a caller that resolved the layout before a
  promotion still gets correct rows. The originals stay unchanged for full branches.

Like revision `0008`, the upgrade refuses to run while `database.application_role` may create
objects in the schema of the tables or of pg_trgm.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-21 18:00:00.000000
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
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


_checks = _revision_module("0008_definer_argument_checks.py")
_reads = _checks._reads

HELPER = "lb_overlay_rows(text, text, text, text)"
SIGNATURES = (
    "lb_search_overlay(text, text, text[], regconfig, text, integer, integer, real, real)",
    "lb_links_in_overlay(text, text, text, text, integer, text, text, text)",
    "lb_neighborhood_overlay(text, text, jsonb, integer, jsonb, integer, integer, integer)",
    "lb_query_by_attribute_overlay(text, text, text, text, text, text, text, integer, text, text)",
)

_RECORD_COLUMNS = (
    "domain_id",
    "branch",
    "kind_id",
    "key",
    "values",
    "derived",
    "title",
    "search",
    "object_hash",
    "lineage",
    "updated_revision",
    "updated_at",
    "revision_generation",
    "deleted",
)
_LINK_COLUMNS = (
    "domain_id",
    "branch",
    "relation_id",
    "from_key",
    "to_key",
    "values",
    "object_hash",
    "lineage",
    "revision_generation",
    "deleted",
)


def _array(columns: Sequence[str]) -> str:
    return "ARRAY[" + ", ".join(f"'{c}'" for c in columns) + "]"


LB_OVERLAY_ROWS = f"""
CREATE FUNCTION lb_overlay_rows(
    p_domain text, p_branch text, p_table text, p_tail text DEFAULT NULL)
RETURNS text
LANGUAGE plpgsql STABLE
SET search_path = {{search_path}}
AS $fn$
DECLARE
    v_columns text[];
    v_key text[];
    v_base text;
    v_generation bigint;
    v_o text := CASE WHEN p_tail IS NULL THEN 'o' ELSE 'm' END;
    v_s text := CASE WHEN p_tail IS NULL THEN 's' ELSE 'm' END;
    v_own text;
    v_from_base text;
    v_shadow text;
BEGIN
    IF p_table = 'mat_records' THEN
        v_columns := {_array(_RECORD_COLUMNS)};
        v_key := ARRAY['kind_id', 'key'];
    ELSIF p_table = 'mat_links' THEN
        v_columns := {_array(_LINK_COLUMNS)};
        v_key := ARRAY['relation_id', 'from_key', 'to_key'];
    ELSE
        RAISE EXCEPTION 'unknown materialized table %', p_table USING ERRCODE = '22023';
    END IF;
    SELECT r.base_branch, r.base_generation INTO v_base, v_generation
    FROM refs r
    WHERE r.domain_id = p_domain AND r.name = p_branch AND r.materialization = 'overlay';
    IF v_base IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT string_agg(v_o || '.' || quote_ident(c), ', ' ORDER BY n) INTO v_own
    FROM unnest(v_columns) WITH ORDINALITY AS t(c, n);
    SELECT string_agg(
               CASE WHEN c = 'branch' THEN format('CAST(%L AS text)', p_branch)
                    ELSE v_s || '.' || quote_ident(c) END, ', ' ORDER BY n)
    INTO v_from_base FROM unnest(v_columns) WITH ORDINALITY AS t(c, n);
    SELECT string_agg(format('x.%1$I = %2$s.%1$I', c, v_s), ' AND ' ORDER BY n)
    INTO v_shadow FROM unnest(v_key) WITH ORDINALITY AS t(c, n);
    -- The same rows as lorebase_store.layout.BranchLayout: overlay rows that are not
    -- tombstones, then base rows written at or before the fork that the overlay does not
    -- shadow, while the ref is still that overlay. With `p_tail` (conditions on alias m,
    -- ORDER BY, LIMIT; built by the read functions only) each half ends with it, as
    -- BranchLayout.records_arms does.
    v_own := format(
        'SELECT %1$s FROM %2$I %5$s WHERE %5$s.domain_id = %3$L AND %5$s.branch = %4$L '
        'AND NOT %5$s.deleted', v_own, p_table, p_domain, p_branch, v_o);
    v_from_base := format(
        'SELECT %1$s FROM %2$I %7$s WHERE %7$s.domain_id = %3$L AND %7$s.branch = %4$L '
        'AND %7$s.revision_generation <= %5$s AND EXISTS (SELECT 1 FROM refs f '
        'WHERE f.domain_id = %3$L AND f.name = %6$L AND f.base_branch = %4$L '
        'AND f.base_generation = %5$s OFFSET 0) AND NOT EXISTS (SELECT 1 FROM %2$I x '
        'WHERE x.domain_id = %3$L AND x.branch = %6$L AND %8$s OFFSET 0)',
        v_from_base, p_table, p_domain, v_base, v_generation, p_branch, v_s, v_shadow);
    IF p_tail IS NULL THEN
        RETURN '(' || v_own || ' UNION ALL ' || v_from_base || ')';
    END IF;
    RETURN '((' || v_own || p_tail || ') UNION ALL (' || v_from_base || p_tail || '))';
END
$fn$
"""

_SOURCES = """
    v_records text;
    v_links text;
"""

_RESOLVE = """
    v_records := coalesce(lb_overlay_rows(p_domain, p_branch, 'mat_records'), 'mat_records');
    v_links := coalesce(lb_overlay_rows(p_domain, p_branch, 'mat_links'), 'mat_links');
"""
"""Run after the checks of revision 0008: the branch's rows are resolved only for a Domain in
the transaction scope, through the checked search_path."""

LB_SEARCH_OVERLAY = """
CREATE FUNCTION lb_search_overlay(
    p_domain text, p_branch text, p_kinds text[], p_config regconfig, p_query text,
    p_candidates integer, p_limit integer,
    p_trigram_threshold real DEFAULT NULL, p_trigram_weight real DEFAULT 0)
RETURNS TABLE (kind_id text, key text, title text, score real)
{options}
SET pg_trgm.similarity_threshold = '0.3'
AS $fn$
DECLARE
    v_match text := 'm.search @@ q.ts';
    v_score text := 'ts_rank_cd(c.search, q.ts)';
    v_kinds text := '';
    {sources}
BEGIN
    IF p_candidates < 1 OR p_limit < 1 THEN
        RAISE EXCEPTION 'candidates and limit must be positive' USING ERRCODE = '22023';
    END IF;
    IF p_kinds IS NOT NULL THEN
        v_kinds := ' AND m.kind_id = ANY ($3)';
    END IF;
    IF p_trigram_threshold IS NOT NULL THEN
        PERFORM set_config('pg_trgm.similarity_threshold', p_trigram_threshold::text, true);
        v_match := '(m.search @@ q.ts OR m.title % $5)';
        v_score := v_score || ' + $9 * similarity(coalesce(c.title, ''''), $5)';
    END IF;
    RETURN QUERY EXECUTE format(
        'SELECT c.kind_id, c.key, c.title, CAST(%s AS real) AS score '
        'FROM (SELECT m.kind_id, m.key, m.title, m.search '
        '      FROM %s m, (SELECT websearch_to_tsquery($4, $5) AS ts) q '
        '      WHERE m.domain_id = %L AND m.branch = %L%s AND %s LIMIT $6) AS c, '
        '     (SELECT websearch_to_tsquery($4, $5) AS ts) q '
        'ORDER BY score DESC, c.kind_id, c.key LIMIT $7',
        v_score, v_records, p_domain, p_branch, v_kinds, v_match)
    USING p_domain, p_branch, p_kinds, p_config, p_query, p_candidates, p_limit,
          p_trigram_threshold, p_trigram_weight;
END
$fn$
"""

LB_LINKS_IN_OVERLAY = """
CREATE FUNCTION lb_links_in_overlay(
    p_domain text, p_branch text, p_relation text, p_to_key text, p_limit integer,
    p_after text DEFAULT NULL, p_from_kind text DEFAULT NULL, p_attribute text DEFAULT NULL)
RETURNS TABLE (relation_id text, from_key text, to_key text, "values" jsonb,
               object_hash bytea)
{options}
AS $fn$
DECLARE
    v_after text := '';
    {sources}
BEGIN
    IF p_limit < 1 THEN
        RAISE EXCEPTION 'limit must be positive' USING ERRCODE = '22023';
    END IF;
    IF (p_from_kind IS NULL) <> (p_attribute IS NULL) THEN
        RAISE EXCEPTION 'a foreign-key Relation needs both its from Kind and its Attribute'
            USING ERRCODE = '22023';
    END IF;
    IF p_attribute IS NULL THEN
        IF p_after IS NOT NULL THEN
            v_after := ' AND l.from_key > $5';
        END IF;
        RETURN QUERY EXECUTE format(
            'SELECT l.relation_id, l.from_key, l.to_key, l."values", l.object_hash '
            'FROM %s l WHERE l.domain_id = %L AND l.branch = %L '
            'AND l.relation_id = $3 AND l.to_key = $4%s ORDER BY l.from_key LIMIT $6',
            v_links, p_domain, p_branch, v_after)
        USING p_domain, p_branch, p_relation, p_to_key, p_after, p_limit;
    ELSE
        IF p_after IS NOT NULL THEN
            v_after := ' AND m.key > $5';
        END IF;
        RETURN QUERY EXECUTE format(
            'SELECT CAST(%L AS text), m.key, CAST($4 AS text), CAST(''{{}}'' AS jsonb), '
            'CAST(NULL AS bytea) FROM %s m '
            'WHERE m.domain_id = %L AND m.branch = %L AND m.kind_id = %L '
            'AND (m."values" ->> %L) = $4%s ORDER BY m.key LIMIT $6',
            p_relation, v_records, p_domain, p_branch, p_from_kind, p_attribute, v_after)
        USING p_domain, p_branch, p_relation, p_to_key, p_after, p_limit;
    END IF;
END
$fn$
"""

LB_QUERY_BY_ATTRIBUTE_OVERLAY = """
CREATE FUNCTION lb_query_by_attribute_overlay(
    p_domain text, p_branch text, p_kind text, p_attribute text, p_type text, p_op text,
    p_value text, p_limit integer, p_after_value text DEFAULT NULL,
    p_after_key text DEFAULT NULL)
RETURNS TABLE ({record_columns})
{options}
AS $fn$
DECLARE
    v_raw text := format('(m."values" ->> %L)', p_attribute);
    v_expression text;
    v_value text;
    v_op text;
    v_sql text;
    v_tail text;
    v_order text;
    v_rows text;
BEGIN
    IF p_limit < 1 THEN
        RAISE EXCEPTION 'limit must be positive' USING ERRCODE = '22023';
    END IF;
    v_op := CASE p_op WHEN 'eq' THEN '=' WHEN 'lt' THEN '<' WHEN 'lte' THEN '<='
                      WHEN 'gt' THEN '>' WHEN 'gte' THEN '>=' END;
    IF v_op IS NULL THEN
        RAISE EXCEPTION 'unknown comparison %', p_op USING ERRCODE = '22023';
    END IF;
    IF p_type = 'text' THEN
        v_expression := v_raw;
        v_value := 'CAST(%s AS text)';
    ELSIF p_type = 'md5' THEN
        v_expression := 'md5' || v_raw;
        v_value := 'md5(CAST(%s AS text))';
    ELSIF p_type IN ('bigint', 'numeric', 'double precision', 'boolean') THEN
        v_expression := format('(%s::%s)', v_raw, p_type);
        v_value := 'CAST(CAST(%s AS text) AS ' || p_type || ')';
    ELSE
        RAISE EXCEPTION 'unknown index type %', p_type USING ERRCODE = '22023';
    END IF;
    -- Conditions, order and limit, applied inside each half of an overlay's rows (each
    -- half reads at most one page, the base half through the base's partial index).
    v_tail := format(' AND m.kind_id = %L AND %s %s %s', p_kind, v_expression, v_op,
                     format(v_value, '$1'));
    IF p_op = 'eq' THEN
        IF p_after_key IS NOT NULL THEN
            v_tail := v_tail || ' AND m.key > $3';
        END IF;
        v_order := 'm.key';
    ELSE
        IF p_after_key IS NOT NULL THEN
            v_tail := v_tail || format(
                ' AND (%s, m.key) > (%s, $3)', v_expression, format(v_value, '$2'));
        END IF;
        v_order := v_expression || ', m.key';
    END IF;
    v_tail := v_tail || ' ORDER BY ' || v_order || ' LIMIT $4';
    v_rows := lb_overlay_rows(p_domain, p_branch, 'mat_records', v_tail);
    IF v_rows IS NULL THEN
        v_rows := format('(SELECT * FROM mat_records m WHERE m.domain_id = %L '
                         'AND m.branch = %L%s) AS m', p_domain, p_branch, v_tail);
    ELSE
        v_rows := v_rows || ' AS m';
    END IF;
    v_sql := 'SELECT m.kind_id, m.key, m."values", m.title, m.object_hash, m.lineage, '
             'm.updated_revision, m.updated_at FROM ' || v_rows
             || ' ORDER BY ' || v_order || ' LIMIT $4';
    RETURN QUERY EXECUTE v_sql USING p_value, p_after_value, p_after_key, p_limit;
END
$fn$
"""

LB_NEIGHBORHOOD_OVERLAY = """
CREATE FUNCTION lb_neighborhood_overlay(
    p_domain text, p_branch text, p_seeds jsonb, p_depth integer, p_relations jsonb,
    p_fanout integer, p_max_steps integer, p_max_nodes integer)
RETURNS TABLE (item text, depth integer, {record_columns}, relation_id text, from_key text,
               to_key text, link_values jsonb, link_hash bytea)
{options}
AS $fn$
DECLARE
    {sources}
    v_parts text[] := ARRAY[]::text[];
    v_relation jsonb;
    v_step text;
    v_join text;
    v_nulls text := 'CAST(NULL AS text), CAST(NULL AS text), CAST(NULL AS text), '
                    'CAST(NULL AS text), CAST(NULL AS text), CAST(NULL AS jsonb), '
                    'CAST(NULL AS bytea)';
BEGIN
    IF p_depth < 1 OR p_fanout < 1 OR p_max_steps < 1 OR p_max_nodes < 1 THEN
        RAISE EXCEPTION 'depth and caps must be positive' USING ERRCODE = '22023';
    END IF;
    IF jsonb_typeof(p_seeds) <> 'array' OR jsonb_typeof(p_relations) <> 'object' THEN
        RAISE EXCEPTION 'seeds must be an array and relations an object'
            USING ERRCODE = '22023';
    END IF;
    -- Lateral, so that the Relation and the frontier key reach both halves of an overlay's
    -- Links as index conditions (OFFSET 0 keeps it a subquery).
    v_join := format(
        'CROSS JOIN LATERAL (SELECT * FROM %s l WHERE l.domain_id = %L AND l.branch = %L '
        'AND l.relation_id = r.relation_id AND l.@near@ = w.key OFFSET 0) AS l',
        v_links, p_domain, p_branch);
    IF jsonb_array_length(coalesce(p_relations -> 'out', '[]')) > 0 THEN
        v_parts := v_parts || (
            'SELECT r.to_kind, l.to_key, l.relation_id, r.from_kind, l.from_key, r.to_kind, '
            'l.to_key, l."values", l.object_hash FROM out_rels r '
            || replace(v_join, '@near@', 'from_key')
            || ' WHERE r.from_kind = w.kind_id LIMIT $3');
    END IF;
    IF jsonb_array_length(coalesce(p_relations -> 'in', '[]')) > 0 THEN
        v_parts := v_parts || (
            'SELECT r.from_kind, l.from_key, l.relation_id, r.from_kind, l.from_key, '
            'r.to_kind, l.to_key, l."values", l.object_hash FROM in_rels r '
            || replace(v_join, '@near@', 'to_key')
            || ' WHERE r.to_kind = w.kind_id LIMIT $3');
    END IF;
    IF jsonb_array_length(coalesce(p_relations -> 'fk_out', '[]')) > 0 THEN
        v_parts := v_parts || format(
            'SELECT f.to_kind, m."values" ->> f.attribute, f.relation_id, f.from_kind, '
            'm.key, f.to_kind, m."values" ->> f.attribute, CAST(''{{}}'' AS jsonb), '
            'CAST(NULL AS bytea) FROM fk_rels f JOIN %s m ON m.domain_id = %L '
            'AND m.branch = %L AND m.kind_id = w.kind_id AND m.key = w.key '
            'WHERE f.from_kind = w.kind_id AND m."values" ->> f.attribute IS NOT NULL '
            'LIMIT $3', v_records, p_domain, p_branch);
    END IF;
    FOR v_relation IN
        SELECT e FROM jsonb_array_elements(coalesce(p_relations -> 'fk_in', '[]')) AS e
    LOOP
        IF (v_relation ->> 0) IS NULL OR (v_relation ->> 1) IS NULL
           OR (v_relation ->> 2) IS NULL OR (v_relation ->> 3) IS NULL THEN
            RAISE EXCEPTION 'a foreign-key Relation is [id, from Kind, to Kind, Attribute]'
                USING ERRCODE = '22023';
        END IF;
        v_parts := v_parts || format(
            'SELECT CAST(%1$L AS text), key, CAST(%2$L AS text), CAST(%1$L AS text), key, '
            'CAST(%3$L AS text), w.key, CAST(''{{}}'' AS jsonb), CAST(NULL AS bytea) '
            'FROM %7$s mat_records WHERE w.kind_id = %3$L AND domain_id = %4$L '
            'AND branch = %5$L AND kind_id = %1$L AND ("values" ->> %6$L) = w.key LIMIT $3',
            v_relation ->> 1, v_relation ->> 0, v_relation ->> 2, p_domain, p_branch,
            v_relation ->> 3, v_records);
    END LOOP;
    IF cardinality(v_parts) = 0 THEN
        v_step := 'SELECT w.depth + 1, w.kind_id, w.key, w.kind_id, w.key, ' || v_nulls
                  || ' FROM walk w WHERE false';
    ELSE
        v_step := 'SELECT w.depth + 1, w.kind_id, w.key, n.* FROM walk w '
                  'CROSS JOIN LATERAL (SELECT * FROM ('
                  || (SELECT string_agg('(' || p.part || ')', ' UNION ALL ' ORDER BY p.n)
                      FROM unnest(v_parts) WITH ORDINALITY AS p(part, n))
                  || ') AS hop LIMIT $3) AS n WHERE w.depth < $2';
    END IF;
    RETURN QUERY EXECUTE format($sql$
    WITH RECURSIVE
    out_rels(relation_id, from_kind, to_kind) AS (
        SELECT e ->> 0, e ->> 1, e ->> 2 FROM jsonb_array_elements($4 -> 'out') AS e),
    in_rels(relation_id, from_kind, to_kind) AS (
        SELECT e ->> 0, e ->> 1, e ->> 2 FROM jsonb_array_elements($4 -> 'in') AS e),
    fk_rels(relation_id, from_kind, to_kind, attribute) AS (
        SELECT e ->> 0, e ->> 1, e ->> 2, e ->> 3
        FROM jsonb_array_elements($4 -> 'fk_out') AS e),
    walk(depth, via_kind, via_key, kind_id, key, relation_id, from_kind, from_key, to_kind,
         to_key, link_values, link_hash) AS (
        SELECT 0, CAST(NULL AS text), CAST(NULL AS text), s ->> 0, s ->> 1, %1$s
        FROM jsonb_array_elements($1) AS s
        UNION ALL
        %2$s
    ) CYCLE kind_id, key SET is_cycle USING path,
    walked AS MATERIALIZED (
        SELECT depth, via_kind, via_key, kind_id, key, relation_id, from_kind, from_key,
               to_kind, to_key, link_values, link_hash
        FROM walk LIMIT $5),
    nodes AS MATERIALIZED (
        SELECT kind_id, key, min(depth) AS depth FROM walked GROUP BY kind_id, key
        ORDER BY min(depth), kind_id, key LIMIT $6 + 1),
    links AS (
        SELECT DISTINCT ON (relation_id, from_key, to_key) relation_id, from_key, to_key,
               link_values, link_hash
        FROM walked w
        WHERE relation_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM nodes n WHERE n.kind_id = w.from_kind AND n.key = w.from_key)
          AND EXISTS (SELECT 1 FROM nodes n WHERE n.kind_id = w.to_kind AND n.key = w.to_key))
    SELECT 'record', n.depth, m.kind_id, m.key, m."values", m.title, m.object_hash, m.lineage,
           m.updated_revision, m.updated_at, CAST(NULL AS text), CAST(NULL AS text),
           CAST(NULL AS text), CAST(NULL AS jsonb), CAST(NULL AS bytea)
    FROM nodes n
    CROSS JOIN LATERAL (
        SELECT * FROM %5$s m WHERE m.domain_id = %3$L AND m.branch = %4$L
                               AND m.kind_id = n.kind_id AND m.key = n.key LIMIT 1) AS m
    UNION ALL
    SELECT 'link', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
           relation_id, from_key, to_key, link_values, link_hash
    FROM links
    UNION ALL
    SELECT 'steps', CAST((SELECT count(*) FROM walked) AS integer), NULL, NULL, NULL, NULL,
           NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
    UNION ALL
    SELECT 'fanout', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
           NULL, NULL
    WHERE EXISTS (
        SELECT 1 FROM walked WHERE depth > 0 GROUP BY depth, via_kind, via_key
        HAVING count(DISTINCT (relation_id, from_key, to_key)) >= $3)
    $sql$, v_nulls, v_step, p_domain, p_branch, v_records)
    USING p_seeds, p_depth, p_fanout, p_relations, p_max_steps, p_max_nodes;
END
$fn$
"""


COUNTS = {
    LB_SEARCH_OVERLAY: ("p_candidates", "p_limit"),
    LB_LINKS_IN_OVERLAY: ("p_limit",),
    LB_QUERY_BY_ATTRIBUTE_OVERLAY: ("p_limit",),
    LB_NEIGHBORHOOD_OVERLAY: ("p_depth", "p_fanout", "p_max_steps", "p_max_nodes"),
}
"""Each overlay template and its count arguments, as in revision 0008."""


def _columns() -> None:
    op.add_column("refs", sa.Column("base_branch", sa.Text(), nullable=True))
    op.add_column("refs", sa.Column("base_revision", sa.LargeBinary(), nullable=True))
    op.add_column("refs", sa.Column("base_generation", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_refs_domain_id_base_branch_refs"),
        "refs",
        "refs",
        ["domain_id", "base_branch"],
        ["domain_id", "name"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        op.f("fk_refs_domain_id_base_revision_revisions"),
        "refs",
        "revisions",
        ["domain_id", "base_revision"],
        ["domain_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_check_constraint(
        op.f("ck_refs_overlay_base"),
        "refs",
        "(materialization = 'overlay') = (base_branch IS NOT NULL "
        "AND base_revision IS NOT NULL AND base_generation IS NOT NULL)",
    )
    op.create_index(
        "ix_refs_domain_id_base_branch",
        "refs",
        ["domain_id", "base_branch"],
        unique=False,
        postgresql_where=sa.text("base_branch IS NOT NULL"),
    )
    for table in ("mat_records", "mat_links"):
        op.add_column(
            table,
            sa.Column(
                "revision_generation", sa.BigInteger(), server_default=sa.text("0"), nullable=False
            ),
        )
        op.add_column(
            table,
            sa.Column("deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        )


def upgrade() -> None:
    role = _reads._application_role()
    if role is not None:
        exists = (
            op.get_bind()
            .execute(
                sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :r)"), {"r": role}
            )
            .scalar()
        )
        if not exists:
            raise RuntimeError(
                f"database.application_role names {role!r}, which does not exist; create the "
                "role first or leave the setting empty"
            )
        _checks._refuse_writable_schemas(role)
    _columns()
    search_path = _reads._search_path()
    op.execute(LB_OVERLAY_ROWS.format(search_path=search_path))
    op.execute(f"REVOKE ALL ON FUNCTION {HELPER} FROM PUBLIC")
    options = _reads._OPTIONS.format(search_path=search_path).strip()
    for template, counts in COUNTS.items():
        guards = _checks._guards(search_path, counts)
        if "{sources}" in template:
            guards += _RESOLVE
        statement = _checks._create_or_replace(template, guards)
        op.execute(
            statement.format(
                options=options, record_columns=_reads._RECORD_COLUMNS, sources=_SOURCES.strip()
            )
        )
    for signature in SIGNATURES:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    if role is not None:
        quoted = op.get_bind().execute(sa.text("SELECT quote_ident(:r)"), {"r": role}).scalar()
        for signature in SIGNATURES:
            op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {quoted}")


def downgrade() -> None:
    for signature in reversed(SIGNATURES):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute(f"DROP FUNCTION IF EXISTS {HELPER}")
    for table in ("mat_links", "mat_records"):
        op.drop_column(table, "deleted")
        op.drop_column(table, "revision_generation")
    op.drop_index("ix_refs_domain_id_base_branch", table_name="refs")
    op.drop_constraint(op.f("ck_refs_overlay_base"), "refs", type_="check")
    op.drop_constraint(
        op.f("fk_refs_domain_id_base_revision_revisions"), "refs", type_="foreignkey"
    )
    op.drop_constraint(op.f("fk_refs_domain_id_base_branch_refs"), "refs", type_="foreignkey")
    op.drop_column("refs", "base_generation")
    op.drop_column("refs", "base_revision")
    op.drop_column("refs", "base_branch")
