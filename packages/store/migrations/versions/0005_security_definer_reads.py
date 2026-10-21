"""Security definer read functions (design 8.7, store schema notes).

Under row-level security Postgres evaluates the `domain_isolation` policy before any condition
built from functions it does not mark leakproof (`->>`, casts, `@@`, `%`), so those conditions
cannot drive an index scan. The four read paths that depend on them get a function each that
runs as its owner (the migration role) and pins the Domain it is given in every predicate:

- `lb_search`: full-text search over a bounded candidate set, with optional trigram matching
  of titles;
- `lb_links_in`: Links pointing at one Record along one Relation, stored or foreign-key backed;
- `lb_neighborhood`: the recursive neighborhood walk of `lorebase_store.traverse`;
- `lb_query_by_attribute`: comparisons served by the per-Attribute partial indexes.

Each function is `STABLE`, `SECURITY DEFINER`, has a fixed `search_path` and runs without
JIT compilation. Statements are
built with `format()` (`%L` literals, whitelisted keywords) and run with `EXECUTE ... USING`,
so every call is planned with its Domain, branch and Kind literals and matches the partial
index predicates. The functions never read `lorebase.domain_ids`: the caller (a store
primitive) passes the Domain it already authorized. EXECUTE is revoked from PUBLIC and granted
to `database.application_role` when set.

The functions skip row-level security only when their owner is exempt from it (a superuser,
or a role with BYPASSRLS); otherwise they return the same rows with the policy applied.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-21 12:00:00.000000
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger(__name__)

SIGNATURES = (
    "lb_search(text, text, text[], regconfig, text, integer, integer, real, real)",
    "lb_links_in(text, text, text, text, integer, text, text, text)",
    "lb_neighborhood(text, text, jsonb, integer, jsonb, integer, integer, integer)",
    "lb_query_by_attribute(text, text, text, text, text, text, text, integer, text, text)",
)

_OPTIONS = """
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = {search_path}
SET jit = off
"""
"""JIT compilation costs more than these short reads (tens of milliseconds for the recursive
neighborhood statement, whose row estimates are inflated by `jsonb_array_elements`)."""

_RECORD_COLUMNS = (
    'kind_id text, key text, "values" jsonb, title text, object_hash bytea, lineage jsonb, '
    "updated_revision bytea, updated_at timestamptz"
)

LB_SEARCH = """
CREATE FUNCTION lb_search(
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
BEGIN
    IF p_candidates < 1 OR p_limit < 1 THEN
        RAISE EXCEPTION 'candidates and limit must be positive' USING ERRCODE = '22023';
    END IF;
    IF p_kinds IS NOT NULL THEN
        v_kinds := ' AND m.kind_id = ANY ($3)';
    END IF;
    IF p_trigram_threshold IS NOT NULL THEN
        -- Local to this call: the SET clause above restores the setting on exit.
        PERFORM set_config('pg_trgm.similarity_threshold', p_trigram_threshold::text, true);
        v_match := '(m.search @@ q.ts OR m.title % $5)';
        v_score := v_score || ' + $9 * similarity(coalesce(c.title, ''''), $5)';
    END IF;
    RETURN QUERY EXECUTE format(
        'SELECT c.kind_id, c.key, c.title, CAST(%s AS real) AS score '
        'FROM (SELECT m.kind_id, m.key, m.title, m.search '
        '      FROM mat_records m, (SELECT websearch_to_tsquery($4, $5) AS ts) q '
        '      WHERE m.domain_id = %L AND m.branch = %L%s AND %s LIMIT $6) AS c, '
        '     (SELECT websearch_to_tsquery($4, $5) AS ts) q '
        'ORDER BY score DESC, c.kind_id, c.key LIMIT $7',
        v_score, p_domain, p_branch, v_kinds, v_match)
    USING p_domain, p_branch, p_kinds, p_config, p_query, p_candidates, p_limit,
          p_trigram_threshold, p_trigram_weight;
END
$fn$
"""

LB_LINKS_IN = """
CREATE FUNCTION lb_links_in(
    p_domain text, p_branch text, p_relation text, p_to_key text, p_limit integer,
    p_after text DEFAULT NULL, p_from_kind text DEFAULT NULL, p_attribute text DEFAULT NULL)
RETURNS TABLE (relation_id text, from_key text, to_key text, "values" jsonb,
               object_hash bytea)
{options}
AS $fn$
DECLARE
    v_after text := '';
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
            'FROM mat_links l WHERE l.domain_id = %L AND l.branch = %L '
            'AND l.relation_id = $3 AND l.to_key = $4%s ORDER BY l.from_key LIMIT $6',
            p_domain, p_branch, v_after)
        USING p_domain, p_branch, p_relation, p_to_key, p_after, p_limit;
    ELSE
        IF p_after IS NOT NULL THEN
            v_after := ' AND m.key > $5';
        END IF;
        -- Domain, branch, Kind and Attribute are literals: they match the partial index.
        RETURN QUERY EXECUTE format(
            'SELECT CAST(%L AS text), m.key, CAST($4 AS text), CAST(''{{}}'' AS jsonb), '
            'CAST(NULL AS bytea) FROM mat_records m '
            'WHERE m.domain_id = %L AND m.branch = %L AND m.kind_id = %L '
            'AND (m."values" ->> %L) = $4%s ORDER BY m.key LIMIT $6',
            p_relation, p_domain, p_branch, p_from_kind, p_attribute, v_after)
        USING p_domain, p_branch, p_relation, p_to_key, p_after, p_limit;
    END IF;
END
$fn$
"""

LB_QUERY_BY_ATTRIBUTE = """
CREATE FUNCTION lb_query_by_attribute(
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
BEGIN
    IF p_limit < 1 THEN
        RAISE EXCEPTION 'limit must be positive' USING ERRCODE = '22023';
    END IF;
    v_op := CASE p_op WHEN 'eq' THEN '=' WHEN 'lt' THEN '<' WHEN 'lte' THEN '<='
                      WHEN 'gt' THEN '>' WHEN 'gte' THEN '>=' END;
    IF v_op IS NULL THEN
        RAISE EXCEPTION 'unknown comparison %', p_op USING ERRCODE = '22023';
    END IF;
    -- The expressions repeat lorebase_store.indexes.IndexSpec.expression and value_sql.
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
    v_sql := format(
        'SELECT m.kind_id, m.key, m."values", m.title, m.object_hash, m.lineage, '
        'm.updated_revision, m.updated_at FROM mat_records m '
        'WHERE m.domain_id = %L AND m.branch = %L AND m.kind_id = %L AND %s %s %s',
        p_domain, p_branch, p_kind, v_expression, v_op, format(v_value, '$1'));
    IF p_op = 'eq' THEN
        IF p_after_key IS NOT NULL THEN
            v_sql := v_sql || ' AND m.key > $3';
        END IF;
        v_sql := v_sql || ' ORDER BY m.key LIMIT $4';
    ELSE
        IF p_after_key IS NOT NULL THEN
            v_sql := v_sql || format(
                ' AND (%s, m.key) > (%s, $3)', v_expression, format(v_value, '$2'));
        END IF;
        v_sql := v_sql || format(' ORDER BY %s, m.key LIMIT $4', v_expression);
    END IF;
    RETURN QUERY EXECUTE v_sql USING p_value, p_after_value, p_after_key, p_limit;
END
$fn$
"""

LB_NEIGHBORHOOD = """
CREATE FUNCTION lb_neighborhood(
    p_domain text, p_branch text, p_seeds jsonb, p_depth integer, p_relations jsonb,
    p_fanout integer, p_max_steps integer, p_max_nodes integer)
RETURNS TABLE (item text, depth integer, {record_columns}, relation_id text, from_key text,
               to_key text, link_values jsonb, link_hash bytea)
{options}
AS $fn$
DECLARE
    v_parts text[] := ARRAY[]::text[];
    v_relation jsonb;
    v_step text;
    v_links text := format(
        'JOIN mat_links l ON l.domain_id = %L AND l.branch = %L '
        'AND l.relation_id = r.relation_id', p_domain, p_branch);
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
    -- One lateral source per kind of hop, as in lorebase_store.traverse.
    IF jsonb_array_length(coalesce(p_relations -> 'out', '[]')) > 0 THEN
        v_parts := v_parts || (
            'SELECT r.to_kind, l.to_key, l.relation_id, r.from_kind, l.from_key, r.to_kind, '
            'l.to_key, l."values", l.object_hash FROM out_rels r ' || v_links ||
            ' AND l.from_key = w.key WHERE r.from_kind = w.kind_id LIMIT $3');
    END IF;
    IF jsonb_array_length(coalesce(p_relations -> 'in', '[]')) > 0 THEN
        v_parts := v_parts || (
            'SELECT r.from_kind, l.from_key, l.relation_id, r.from_kind, l.from_key, '
            'r.to_kind, l.to_key, l."values", l.object_hash FROM in_rels r ' || v_links ||
            ' AND l.to_key = w.key WHERE r.to_kind = w.kind_id LIMIT $3');
    END IF;
    IF jsonb_array_length(coalesce(p_relations -> 'fk_out', '[]')) > 0 THEN
        v_parts := v_parts || format(
            'SELECT f.to_kind, m."values" ->> f.attribute, f.relation_id, f.from_kind, '
            'm.key, f.to_kind, m."values" ->> f.attribute, CAST(''{{}}'' AS jsonb), '
            'CAST(NULL AS bytea) FROM fk_rels f JOIN mat_records m ON m.domain_id = %L '
            'AND m.branch = %L AND m.kind_id = w.kind_id AND m.key = w.key '
            'WHERE f.from_kind = w.kind_id AND m."values" ->> f.attribute IS NOT NULL '
            'LIMIT $3', p_domain, p_branch);
    END IF;
    FOR v_relation IN
        SELECT e FROM jsonb_array_elements(coalesce(p_relations -> 'fk_in', '[]')) AS e
    LOOP
        IF (v_relation ->> 0) IS NULL OR (v_relation ->> 1) IS NULL
           OR (v_relation ->> 2) IS NULL OR (v_relation ->> 3) IS NULL THEN
            RAISE EXCEPTION 'a foreign-key Relation is [id, from Kind, to Kind, Attribute]'
                USING ERRCODE = '22023';
        END IF;
        -- Literal predicate and expression: the per-Attribute index of the from Kind.
        v_parts := v_parts || format(
            'SELECT CAST(%1$L AS text), key, CAST(%2$L AS text), CAST(%1$L AS text), key, '
            'CAST(%3$L AS text), w.key, CAST(''{{}}'' AS jsonb), CAST(NULL AS bytea) '
            'FROM mat_records WHERE w.kind_id = %3$L AND domain_id = %4$L '
            'AND branch = %5$L AND kind_id = %1$L AND ("values" ->> %6$L) = w.key LIMIT $3',
            v_relation ->> 1, v_relation ->> 0, v_relation ->> 2, p_domain, p_branch,
            v_relation ->> 3);
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
    JOIN mat_records m ON m.domain_id = %3$L AND m.branch = %4$L
                      AND m.kind_id = n.kind_id AND m.key = n.key
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
    $sql$, v_nulls, v_step, p_domain, p_branch)
    USING p_seeds, p_depth, p_fanout, p_relations, p_max_steps, p_max_nodes;
END
$fn$
"""


def _search_path() -> str:
    """The migration's schema (where the tables are) and pg_trgm's, pg_temp last."""
    bind = op.get_bind()
    schema = bind.execute(sa.text("SELECT quote_ident(current_schema())")).scalar_one()
    trgm = bind.execute(
        sa.text(
            "SELECT quote_ident(n.nspname) FROM pg_extension e "
            "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'pg_trgm'"
        )
    ).scalar_one_or_none()
    schemas = ["pg_catalog", schema]
    if trgm is not None and trgm not in schemas:
        schemas.append(trgm)
    return ", ".join([*schemas, "pg_temp"])


def _application_role() -> str | None:
    config = op.get_context().config
    if config is None or "database_settings" not in config.attributes:
        raise RuntimeError("run this migration through the Lorebase Alembic environment")
    role: str | None = config.attributes["database_settings"].application_role
    return role


def upgrade() -> None:
    options = _OPTIONS.format(search_path=_search_path()).strip()
    for template in (LB_SEARCH, LB_LINKS_IN, LB_QUERY_BY_ATTRIBUTE, LB_NEIGHBORHOOD):
        op.execute(template.format(options=options, record_columns=_RECORD_COLUMNS))
    for signature in SIGNATURES:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    bind = op.get_bind()
    role = _application_role()
    if role is not None:
        quoted, exists = bind.execute(
            sa.text("SELECT quote_ident(:r), EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :r)"),
            {"r": role},
        ).one()
        if not exists:
            raise RuntimeError(
                f"database.application_role names {role!r}, which does not exist; create the "
                "role first or leave the setting empty"
            )
        for signature in SIGNATURES:
            op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {quoted}")
    exempt = bind.execute(
        sa.text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).scalar_one()
    if not exempt:
        log.warning(
            "the read functions are owned by %s, which is subject to row-level security: they "
            "return correct rows but keep the slower plans (see docs/store-schema-notes.md)",
            bind.execute(sa.text("SELECT current_user")).scalar_one(),
        )


def downgrade() -> None:
    for signature in reversed(SIGNATURES):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
