"""Optional BM25 ranking of lexical search through ParadeDB `pg_search` (docs/search.md#bm25-
ranking; store schema notes).

Nothing here needs the extension: the functions below only name its types and operator inside
statements they build at run time, so this revision runs, and they exist, on any Postgres. The
BM25 index itself is built by `python -m lorebase_store.storage apply` with
`store.storage.bm25_index: true` (`lorebase_store.bm25`), and Keeper ranks with it only with
`search.backend: bm25` once the index is valid.

- `lb_search_terms(search)`: the lexemes of a search vector as whitespace-separated text, each
  repeated at each of its positions and in position order (once when the vector has no
  positions). The BM25 index tokenizes it on whitespace (`pdb.whitespace`, field `terms`), so
  it holds exactly the lexemes full-text search matches (the text search configuration's
  stemming, the searchable Attributes only, sensitive values left out as in the vector), with
  their frequencies and order for phrases.
- `lb_bm25_query_valid(query)`: whether a JSON search query uses only what the store sends:
  `term` and `phrase` on the field `terms`, `boolean` (`must`, `should`, `must_not`, none
  empty) and `all`, at most 32 levels deep.
- `lb_search_bm25` and `lb_search_bm25_overlay`: security definer search, with the options,
  checks and grants of revisions 0008 and 0018 (count ceilings, a Domain in the transaction
  scope, a fixed search_path). The caller translates the query as written (`p_match`) and the
  hits that match only through typo corrections (`p_fixed`) into BM25 queries; a query that is
  not translated exactly (`p_*_exact` false: phrases, a negated phrase) is a superset, ranked
  for its first `p_candidates` matches and rechecked against the search vector. A query that
  matches everything but some words (`all`) leaves out rows without a search vector, as `@@`
  does. Each scan is a top-k scan of the index ordered by BM25 score, ties by Kind and key
  (bytewise); `score` is relative to the best row returned (1), so that it does not carry the
  index-wide term statistics the raw BM25 score is built from. On an overlay
  the overlay's own rows and the base's rows as of the fork point are scanned apart; the base's
  scan reads as many more rows as the overlay holds of the searched Kinds (at most
  `p_candidates` more), so the rows it shadows cannot push a match out. `truncated` is true on
  every row when a scan that filled its limit may have left out a row that would rank among
  those returned. When the index cannot serve the call (dropped meanwhile, the library not
  loaded), the functions return one row without a Kind that holds the error (SQLSTATE in
  `key`, message in `title`) instead of failing the caller's transaction; the store then ranks
  the default way.

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-30 09:00:00.000000
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

revision: str = "0037"
down_revision: str | Sequence[str] | None = "0036"
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


_lexicon = _revision_module("0018_search_lexicon.py")
_checks = _lexicon._checks
_reads = _lexicon._reads

INDEX = "ix_mat_records_bm25"
"""Built by `storage apply` (`lorebase_store.bm25.INDEX`); dropped with its functions."""

SEARCH_TERMS = """
CREATE FUNCTION lb_search_terms(p_search tsvector) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
BEGIN ATOMIC
    SELECT pg_catalog.string_agg(u.lexeme, ' ' ORDER BY p.position, u.lexeme COLLATE "C")
    FROM pg_catalog.unnest(p_search) AS u(lexeme, positions, weights)
    CROSS JOIN LATERAL pg_catalog.unnest(
        CASE WHEN coalesce(pg_catalog.cardinality(u.positions), 0)
                  OPERATOR(pg_catalog.=) 0
             THEN CAST(ARRAY[0] AS pg_catalog.int2[]) ELSE u.positions END) AS p(position);
END
"""
"""The indexed text of a search vector. A standard SQL body: names resolve when it is created,
never through a caller's path."""

QUERY_VALID = """
CREATE FUNCTION lb_bm25_query_valid(p_query jsonb, p_depth integer DEFAULT 0) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = {search_path}
AS $fn$
DECLARE
    v_key text;
    v_value jsonb;
    v_clause text;
    v_items jsonb;
BEGIN
    IF p_query IS NULL OR p_depth > 32 OR jsonb_typeof(p_query) <> 'object'
       OR (SELECT count(*) FROM jsonb_object_keys(p_query)) <> 1 THEN
        RETURN false;
    END IF;
    SELECT e.key, e.value INTO v_key, v_value FROM jsonb_each(p_query) AS e;
    IF v_key = 'all' THEN
        RETURN v_value = 'null'::jsonb;
    ELSIF v_key = 'term' THEN
        RETURN jsonb_typeof(v_value) = 'object'
           AND ARRAY(SELECT k FROM jsonb_object_keys(v_value) AS k ORDER BY 1)
               = ARRAY['field', 'value']
           AND v_value -> 'field' = '"terms"'::jsonb
           AND jsonb_typeof(v_value -> 'value') = 'string';
    ELSIF v_key = 'phrase' THEN
        RETURN jsonb_typeof(v_value) = 'object'
           AND ARRAY(SELECT k FROM jsonb_object_keys(v_value) AS k ORDER BY 1)
               = ARRAY['field', 'phrases', 'slop']
           AND v_value -> 'field' = '"terms"'::jsonb
           AND jsonb_typeof(v_value -> 'phrases') = 'array'
           AND jsonb_array_length(v_value -> 'phrases') BETWEEN 1 AND 64
           AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v_value -> 'phrases') AS p(x)
                           WHERE jsonb_typeof(p.x) <> 'string')
           AND v_value -> 'slop' IN ('0'::jsonb, '1'::jsonb);
    ELSIF v_key <> 'boolean' OR jsonb_typeof(v_value) <> 'object'
          OR NOT EXISTS (SELECT 1 FROM jsonb_object_keys(v_value)) THEN
        RETURN false;
    END IF;
    FOR v_clause, v_items IN SELECT e.key, e.value FROM jsonb_each(v_value) AS e LOOP
        IF v_clause NOT IN ('must', 'should', 'must_not') OR jsonb_typeof(v_items) <> 'array'
           OR jsonb_array_length(v_items) NOT BETWEEN 1 AND 256 THEN
            RETURN false;
        END IF;
        IF EXISTS (SELECT 1 FROM jsonb_array_elements(v_items) AS i(x)
                   WHERE NOT lb_bm25_query_valid(i.x, p_depth + 1)) THEN
            RETURN false;
        END IF;
    END LOOP;
    RETURN true;
END
$fn$
"""
"""The queries `lorebase_store.bm25` builds and nothing else: the definer functions refuse any
other (another field, another query type of the extension), whoever calls them."""

LB_SEARCH_BM25 = """
CREATE FUNCTION {name}(
    p_domain text, p_branch text, p_kinds text[], p_config regconfig, p_query text,
    p_candidates integer, p_limit integer, p_match jsonb, p_match_exact boolean,
    p_words text[] DEFAULT NULL, p_replacements text[] DEFAULT NULL, p_weight real DEFAULT 1,
    p_fixed jsonb DEFAULT NULL, p_fixed_exact boolean DEFAULT true)
RETURNS TABLE (kind_id text, key text, title text, score real, exact boolean,
               truncated boolean)
{{options}}
AS $fn$
DECLARE
    v_kinds text := '';
    v_ts tsquery;
    v_fix tsquery;
    v_word text;
    v_replacements text[];
    v_base text;
    v_generation bigint;
    v_shadow bigint := 0;
    v_halves text[] := CAST(ARRAY[] AS text[]);
    v_rows text[] := CAST(ARRAY[] AS text[]);
    v_stats text[] := CAST(ARRAY[] AS text[]);
    v_arm integer;
    v_half integer;
    v_n integer := 0;
    v_exact boolean;
    v_label text;
    v_param text;
    v_recheck text;
    v_branch text;
    v_filter text;
    v_limit bigint;
    v_present text;
    v_state text;
    v_message text;
BEGIN
    IF cardinality(p_words) IS DISTINCT FROM cardinality(p_replacements) THEN
        RAISE EXCEPTION 'p_words and p_replacements must have the same length'
            USING ERRCODE = '22023';
    END IF;
    IF p_weight IS NULL OR p_weight < 0 THEN
        RAISE EXCEPTION 'p_weight must not be negative' USING ERRCODE = '22023';
    END IF;
    IF p_match_exact IS NULL OR p_fixed_exact IS NULL OR NOT lb_bm25_query_valid(p_match)
       OR (p_fixed IS NOT NULL AND NOT lb_bm25_query_valid(p_fixed)) THEN
        RAISE EXCEPTION 'p_match and p_fixed must be search term queries'
            USING ERRCODE = '22023';
    END IF;
    IF p_kinds IS NOT NULL THEN
        v_kinds := ' AND m.kind_id = ANY ($3)';
    END IF;
    v_ts := websearch_to_tsquery(p_config, p_query);
    v_fix := v_ts;
    -- As lb_search_corrected: each corrected lexeme becomes (lexeme | replacement | ...).
    FOR v_word, v_replacements IN
        SELECT t.w, array_agg(t.r ORDER BY t.n)
        FROM unnest(p_words, p_replacements) WITH ORDINALITY AS t(w, r, n)
        GROUP BY t.w ORDER BY t.w
    LOOP
        v_fix := ts_rewrite(v_fix, lb_lexeme_query(v_word),
                            lb_lexeme_query(v_word, v_replacements));
    END LOOP;
    {{resolve}}
    -- Arm 1: the query as written; arm 2: matches only through a correction. Each arm scans
    -- the branch (half 1), or on an overlay its own rows (half 1) and the base's (half 2).
    FOR v_arm IN 1..2 LOOP
        CONTINUE WHEN v_arm = 2 AND (p_fixed IS NULL OR v_fix = v_ts);
        v_exact := CASE WHEN v_arm = 1 THEN p_match_exact ELSE p_fixed_exact END;
        v_param := CASE WHEN v_arm = 1 THEN '$6' ELSE '$7' END;
        v_label := CASE WHEN v_arm = 1 THEN 'true' ELSE 'false' END;
        -- `all` matches rows without a search vector too; `search @@` never does.
        v_present := CASE WHEN jsonb_path_exists(CASE WHEN v_arm = 1 THEN p_match
                                                      ELSE p_fixed END, '$.**."all"')
                          THEN ' AND m.search IS NOT NULL' ELSE '' END;
        v_recheck := CASE WHEN v_exact THEN 'true'
                          WHEN v_arm = 1 THEN 'h.search @@ $4'
                          ELSE 'h.search @@ $5 AND NOT h.search @@ $4' END;
        FOR v_half IN 1..CASE WHEN v_base IS NULL THEN 1 ELSE 2 END LOOP
            v_n := v_n + 1;
            v_limit := CASE WHEN v_exact THEN p_limit ELSE p_candidates END;
            v_filter := v_recheck;
            IF v_base IS NULL THEN
                v_branch := format('m.branch = %L', p_branch);
            ELSIF v_half = 1 THEN
                v_branch := format('m.branch = %L AND NOT m.deleted', p_branch);
            ELSE
                -- Base rows written at or before the fork, while the ref is still that
                -- overlay, that the overlay does not shadow (as lb_overlay_rows): the scan
                -- reads one more row for each row the overlay holds of these Kinds.
                v_branch := format('m.branch = %L AND m.revision_generation <= %s',
                                   v_base, v_generation);
                v_limit := v_limit + least(v_shadow, p_candidates);
                v_filter := v_filter || format(
                    ' AND EXISTS (SELECT 1 FROM refs f WHERE f.domain_id = %1$L '
                    'AND f.name = %2$L AND f.base_branch = %3$L AND f.base_generation = %4$s '
                    'OFFSET 0) AND NOT EXISTS (SELECT 1 FROM mat_records x '
                    'WHERE x.domain_id = %1$L AND x.branch = %2$L AND x.kind_id = h.kind_id '
                    'AND x.key = h.key OFFSET 0)',
                    p_domain, p_branch, v_base, v_generation);
            END IF;
            v_halves := v_halves || format(
                'h%1$s AS MATERIALIZED (SELECT m.kind_id, m.key, m.title, %2$s AS search, '
                'CAST(pdb.score(m.key) AS real) AS raw FROM mat_records m '
                'WHERE m.domain_id = %3$L AND %4$s%5$s%8$s '
                'AND m.key OPERATOR(pg_catalog.@@@) CAST(%6$s AS paradedb.searchqueryinput) '
                'ORDER BY pdb.score(m.key) DESC, m.kind_id COLLATE "C", m.key COLLATE "C" '
                'LIMIT %7$s)',
                v_n, CASE WHEN v_exact THEN 'CAST(NULL AS tsvector)' ELSE 'm.search' END,
                p_domain, v_branch, v_kinds, v_param, v_limit, v_present);
            v_rows := v_rows || format(
                'SELECT h.kind_id, h.key, h.title, h.raw, %1$s AS exact FROM h%2$s h '
                'WHERE %3$s', v_label, v_n, v_filter);
            v_stats := v_stats || format(
                'SELECT %1$s AS exact, count(*) >= %2$s AS filled, min(h.raw) AS floor '
                'FROM h%3$s h', v_label, v_limit, v_n);
        END LOOP;
    END LOOP;
    -- A scan that filled its limit may have left out rows scoring at most its last one: the
    -- result is cut when such a row would rank among those returned (fewer rows than asked
    -- at or above its arm, or a returned row of its arm scoring below that last one).
    BEGIN
    RETURN QUERY EXECUTE
        'WITH ' || array_to_string(v_halves, ', ')
        || ', r AS (' || array_to_string(v_rows, ' UNION ALL ') || ')'
        || ', s AS (' || array_to_string(v_stats, ' UNION ALL ') || ')'
        || ', t AS (SELECT r.kind_id, r.key, r.title, r.raw, r.exact, '
        || 'CAST(CASE WHEN r.exact THEN r.raw ELSE $8 * r.raw END AS real) AS score FROM r '
        || 'ORDER BY r.exact DESC, score DESC, r.raw DESC, r.kind_id COLLATE "C", '
        || 'r.key COLLATE "C" LIMIT $9) '
        || 'SELECT t.kind_id, t.key, t.title, '
        || 'CAST(coalesce(t.score / nullif(max(t.score) OVER (), 0), 0) AS real), t.exact, '
        || 'EXISTS (SELECT 1 FROM s WHERE s.filled AND ((SELECT count(*) FROM t x '
        || 'WHERE x.exact OR NOT s.exact) < $9 '
        || 'OR (SELECT min(x.raw) FROM t x WHERE x.exact = s.exact) < s.floor)) '
        || 'FROM t ORDER BY t.exact DESC, t.score DESC, t.raw DESC, t.kind_id COLLATE "C", '
        || 't.key COLLATE "C"'
    USING p_domain, p_branch, p_kinds, v_ts, v_fix, p_match, p_fixed, p_weight, p_limit;
    EXCEPTION WHEN OTHERS THEN
        -- The index cannot serve the call (dropped since the caller looked, the library not
        -- loaded, a query the extension refuses): a row without a Kind, holding the error,
        -- tells the caller to rank the default way. Cancellation is not caught (OTHERS).
        GET STACKED DIAGNOSTICS v_state = RETURNED_SQLSTATE, v_message = MESSAGE_TEXT;
        RETURN QUERY SELECT CAST(NULL AS text), v_state, v_message, CAST(NULL AS real),
            CAST(NULL AS boolean), CAST(NULL AS boolean);
    END;
END
$fn$
"""

_RESOLVE_OVERLAY = """
    SELECT r.base_branch, r.base_generation INTO v_base, v_generation FROM refs r
    WHERE r.domain_id = p_domain AND r.name = p_branch AND r.materialization = 'overlay';
    IF v_base IS NOT NULL THEN
        EXECUTE 'SELECT count(*) FROM (SELECT 1 FROM mat_records x WHERE x.domain_id = $1 '
                'AND x.branch = $2 AND ($3 IS NULL OR x.kind_id = ANY ($3)) LIMIT $4) AS s'
        INTO v_shadow USING p_domain, p_branch, p_kinds, p_candidates + 1;
    END IF;"""
"""The overlay variant finds the base and the generation it forked at (a branch that is no
longer an overlay is read as a full one) and counts the overlay's rows of the searched Kinds,
tombstones included: each can shadow one base row."""

PARALLEL = "\nSET max_parallel_workers_per_gather = 0"
"""A top-k scan of a few rows costs less than starting workers: with them the planner picked a
parallel scan of the overlay's rows that took 19 ms instead of 1 (1M Records)."""

SEARCH = (
    ("lb_search_bm25", ""),
    ("lb_search_bm25_overlay", _RESOLVE_OVERLAY),
)
ARGUMENTS = (
    "(text, text, text[], regconfig, text, integer, integer, jsonb, boolean, text[], text[], "
    "real, jsonb, boolean)"
)
SIGNATURES = tuple(f"{name}{ARGUMENTS}" for name, _ in SEARCH)
HELPERS = ("lb_bm25_query_valid(jsonb, integer)", "lb_search_terms(tsvector)")


def upgrade() -> None:
    role = _reads._application_role()
    if role is not None:
        _checks._refuse_writable_schemas(role)
    search_path = _reads._search_path()
    op.execute(SEARCH_TERMS)
    op.execute(QUERY_VALID.format(search_path=search_path))
    options = _reads._OPTIONS.format(search_path=search_path).strip() + PARALLEL
    for name, resolve in SEARCH:
        guards = _checks._guards(search_path, ("p_candidates", "p_limit"))
        template = LB_SEARCH_BM25.format(name=name)
        statement = _checks._create_or_replace(template, guards)
        op.execute(statement.format(options=options, resolve=resolve.strip()))
    _lexicon._grant(SIGNATURES)


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    for signature in reversed(SIGNATURES):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute(f"DROP FUNCTION IF EXISTS {HELPERS[0]}")
    # CASCADE: partition indexes of an interrupted build, never attached to INDEX, use it too.
    # The extension stays; an administrator drops it.
    op.execute(f"DROP FUNCTION IF EXISTS {HELPERS[1]} CASCADE")
