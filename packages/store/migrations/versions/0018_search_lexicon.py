"""Typo tolerance through a per-branch word lexicon (design 8.4; store schema notes).

Typo-tolerant search matched whole titles by trigram similarity (`m.title % query` on
`ix_mat_records_branch_title`). The index is lossy: a common word appears in long titles that
are never similar to it, so at 1M Records a search read and rechecked about 150,000 rows. This
revision replaces it with the approach of the pg_trgm documentation for unique words: trigram
matching runs on the distinct words of a branch, and full-text search runs with the corrected
query.

- `mat_lexicon(domain_id, branch, kind_id, word, records)`: per Kind, the lexemes of the
  branch's own search vectors (title and searchable Attributes,
  `store.materialize.text_search_config`) and the number of its rows that hold each, so
  corrections come only from the Kinds a caller may read. Words with a digit or longer than
  64 characters are left out (`lb_lexicon_word`): codes and keys are not typo-corrected.
  Domain-scoped with row-level security like the tables of 0001; rows go with their branch.
  An overlay holds the words of its own rows; its readers add the base's (a superset of what
  it reads, which only costs a candidate that matches nothing).
- Statement-level triggers on `mat_records` (`lb_lexicon_maintain`, transition tables) keep
  it in step with every write of the commit pipeline, copy-on-write, promotion and deletion,
  in the writing transaction: per statement, one aggregate over the rows whose search vector
  changed. Rows of a branch whose ref is gone (a cascade) are skipped. Spent words (count 0)
  are deleted through a small partial index.
- `lb_search_corrections(domain, branch, kinds, config, query, threshold, candidates,
  min_length)`: for each query lexeme that is long enough, has no digit, is not negated and
  is not in the lexicon of the branch's `kinds` (and of its base, for an overlay), its
  `candidates` closest words of those Kinds at least `threshold` similar (trigram GIN index
  on `(domain_id, branch, word)`), best first.
- `lb_search_corrected` and `lb_search_corrected_overlay`: full-text search where each
  corrected lexeme is replaced by itself or its replacements (`ts_rewrite`); matches of the
  query as written come first (`exact`), the others are ranked by `weight` times their rank.
- `lb_lexeme_query(word, alternatives)` builds the tsquery of a lexeme or of its
  alternatives without normalizing them; plain SQL (`store.reads.use_definer_functions:
  false`) uses it too.

The definer functions have the options, checks and grants of revisions 0008 and 0010. The
functions of revision 0005 stay for processes that still call them; `ix_mat_records_branch_
title` is dropped, so their trigram branch no longer has an index.

The backfill reads every search vector once, under the lock that creating the triggers takes
on `mat_records` (writes wait for it).

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-22 21:00:00.000000
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
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


_indexes = _revision_module("0011_branch_scoped_search_index.py")
_overlays = _indexes._overlays
_checks = _indexes._checks
_reads = _indexes._reads

DOMAIN_VISIBLE = (
    "current_setting('lorebase.bypass_rls', true) = 'on' "
    "OR domain_id = ANY (string_to_array(current_setting('lorebase.domain_ids', true), ','))"
)

TITLE_INDEX = _indexes.SWAPS[1][1]
"""`ix_mat_records_branch_title` of revision 0011, dropped here and rebuilt on downgrade."""

LEXICON_WORD = """
CREATE FUNCTION lb_lexicon_word(p_word text) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN p_word OPERATOR(pg_catalog.!~) '[0-9]'
   AND pg_catalog.char_length(p_word) OPERATOR(pg_catalog.<=) 64
"""
"""Whether a lexeme belongs in the lexicon. Qualified names, no SET clause: it is inlined
into the trigger and the read functions, and resolves nothing through a caller's path."""

LEXEME_QUERY = """
CREATE FUNCTION lb_lexeme_query(p_word text, p_alternatives text[] DEFAULT NULL)
RETURNS tsquery
LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN CAST(pg_catalog.array_to_string(ARRAY(
    SELECT '''' OPERATOR(pg_catalog.||) pg_catalog.replace(pg_catalog.replace(
               t.w, '\\', '\\\\'), '''', '''''') OPERATOR(pg_catalog.||) ''''
    FROM pg_catalog.unnest(ARRAY[p_word] OPERATOR(pg_catalog.||)
                           coalesce(p_alternatives, CAST('{}' AS pg_catalog.text[])))
         WITH ORDINALITY AS t(w, n)
    ORDER BY t.n), ' | ') AS pg_catalog.tsquery)
"""
"""`'word'`, or `'word' | 'alternative' | ...`: lexemes quoted as tsquery input (quotes and
backslashes doubled), so they are taken as they are, without a dictionary."""

_ROWS = """
SELECT {row}.domain_id, {row}.branch, {row}.kind_id, w.lexeme AS word, {sign} AS records
FROM {table} {row}{live}
CROSS JOIN LATERAL unnest({row}.search) AS w
WHERE {row}.search IS NOT NULL AND NOT {row}.deleted AND lb_lexicon_word(w.lexeme){unchanged}
"""
_LIVE = " JOIN refs r ON r.domain_id = {row}.domain_id AND r.name = {row}.branch"
"""Rows of a branch whose ref is gone (deleted, the delete cascading) leave no words."""
_UNCHANGED = (
    " AND NOT EXISTS (SELECT 1 FROM {other} x WHERE x.domain_id = {row}.domain_id "
    "AND x.branch = {row}.branch AND x.kind_id = {row}.kind_id AND x.key = {row}.key "
    "AND x.deleted = {row}.deleted AND x.search IS NOT DISTINCT FROM {row}.search)"
)
"""An UPDATE that keeps a row's search vector (most of them) changes none of its words."""


def _rows(table: str, row: str, sign: int, *, live: bool, other: str | None) -> str:
    return _ROWS.format(
        row=row,
        table=table,
        sign=sign,
        live=_LIVE.format(row=row) if live else "",
        unchanged=_UNCHANGED.format(other=other, row=row) if other else "",
    ).strip()


SMALL_STATEMENT_ROWS = 64
"""Statements that change at most this many rows sum their word changes with a cached plan;
larger ones plan the sum for their own row counts (see `_delta`)."""


def _delta(table: str, *arms: str) -> str:
    """The per-word count changes of one statement, into the function's arrays.

    The commit pipeline writes many one-row statements (`executemany`), where planning each
    call would cost more than the work; a cached plan of a small statement, on the other
    hand, would compare every pair of rows of a 10,000-row one. So small statements use the
    plan plpgsql caches and large ones `EXECUTE`, planned for their row counts.
    """
    order = "ORDER BY d.domain_id, d.branch, d.kind_id, d.word"
    select = (
        f"SELECT array_agg(d.domain_id {order}), array_agg(d.branch {order}), "
        f"array_agg(d.kind_id {order}), array_agg(d.word {order}), "
        f"array_agg(d.records {order}) {{into}}"
        "FROM (SELECT x.domain_id, x.branch, x.kind_id, x.word, sum(x.records) AS records "
        "FROM ("
        + " UNION ALL ".join(arms)
        + ") AS x GROUP BY x.domain_id, x.branch, x.kind_id, x.word "
        "HAVING sum(x.records) <> 0) AS d"
    )
    into = "INTO v_domains, v_branches, v_kind_ids, v_words, v_counts"
    return (
        f"SELECT count(*) INTO v_rows FROM {table};\n"
        f"        IF v_rows <= {SMALL_STATEMENT_ROWS} THEN\n"
        f"            {select.format(into=into + ' ')};\n"
        "        ELSE\n"
        f"            EXECUTE $delta${select.format(into='')}$delta$ {into};\n"
        "        END IF;"
    )


INSERTED = _delta("lb_new", _rows("lb_new", "n", 1, live=False, other=None))
UPDATED = _delta(
    "lb_new",
    _rows("lb_new", "n", 1, live=False, other="lb_old"),
    _rows("lb_old", "o", -1, live=True, other="lb_new"),
)
DELETED = _delta("lb_old", _rows("lb_old", "o", -1, live=True, other=None))

LEXICON_MAINTAIN = f"""
CREATE FUNCTION lb_lexicon_maintain() RETURNS trigger
LANGUAGE plpgsql
SET search_path = {{search_path}}
AS $fn$
DECLARE
    v_domains text[];
    v_branches text[];
    v_kind_ids text[];
    v_words text[];
    v_counts bigint[];
    v_rows bigint;
BEGIN
    IF TG_OP = 'INSERT' THEN
        {INSERTED}
    ELSIF TG_OP = 'UPDATE' THEN
        {UPDATED}
    ELSE
        {DELETED}
    END IF;
    IF v_words IS NULL THEN
        RETURN NULL;
    END IF;
    -- One upsert in key order for gains and losses alike (the losses of a branch whose ref
    -- is gone were left out above); writers of one branch hold its lock anyway.
    INSERT INTO mat_lexicon AS l (domain_id, branch, kind_id, word, records)
    SELECT d.domain_id, d.branch, d.kind_id, d.word, d.records
    FROM unnest(v_domains, v_branches, v_kind_ids, v_words, v_counts)
         WITH ORDINALITY AS d(domain_id, branch, kind_id, word, records, n)
    ORDER BY d.n
    ON CONFLICT (domain_id, branch, kind_id, word)
    DO UPDATE SET records = l.records + EXCLUDED.records;
    IF EXISTS (SELECT 1 FROM unnest(v_counts) AS c(records) WHERE c.records < 0) THEN
        DELETE FROM mat_lexicon AS l
        USING (SELECT DISTINCT b.domain_id, b.branch
               FROM unnest(v_domains, v_branches) AS b(domain_id, branch)) AS b
        WHERE l.domain_id = b.domain_id AND l.branch = b.branch AND l.records <= 0;
    END IF;
    RETURN NULL;
END
$fn$
"""

TRIGGERS = (
    ("lb_lexicon_insert", "INSERT", "NEW TABLE AS lb_new"),
    ("lb_lexicon_update", "UPDATE", "OLD TABLE AS lb_old NEW TABLE AS lb_new"),
    ("lb_lexicon_delete", "DELETE", "OLD TABLE AS lb_old"),
)

BACKFILL = """
INSERT INTO mat_lexicon (domain_id, branch, kind_id, word, records)
SELECT m.domain_id, m.branch, m.kind_id, w.lexeme, count(*)
FROM mat_records m CROSS JOIN LATERAL unnest(m.search) AS w
WHERE m.search IS NOT NULL AND NOT m.deleted AND lb_lexicon_word(w.lexeme)
GROUP BY m.domain_id, m.branch, m.kind_id, w.lexeme
"""

LB_SEARCH_CORRECTIONS = """
CREATE FUNCTION lb_search_corrections(
    p_domain text, p_branch text, p_kinds text[], p_config regconfig, p_query text,
    p_threshold real, p_candidates integer, p_min_length integer)
RETURNS TABLE (word text, replacement text, similarity real)
{options}
SET pg_trgm.similarity_threshold = '0.3'
AS $fn$
DECLARE
    v_base text;
    v_word text;
    v_lookup text;
    v_kinds text := '';
BEGIN
    IF p_threshold IS NULL OR NOT (p_threshold > 0 AND p_threshold <= 1) THEN
        RAISE EXCEPTION 'p_threshold must be in (0, 1]' USING ERRCODE = '22023';
    END IF;
    IF p_min_length IS NULL OR p_min_length < 1 THEN
        RAISE EXCEPTION 'p_min_length must be positive' USING ERRCODE = '22023';
    END IF;
    -- Local to this call: the SET clause above restores the setting on exit.
    PERFORM set_config('pg_trgm.similarity_threshold', p_threshold::text, true);
    SELECT r.base_branch INTO v_base FROM refs r
    WHERE r.domain_id = p_domain AND r.name = p_branch AND r.materialization = 'overlay';
    IF p_kinds IS NOT NULL THEN
        v_kinds := ' AND l.kind_id = ANY ($3)';
    END IF;
    -- Domain and branch are literals: index conditions of the trigram index.
    v_lookup := format(
        'SELECT l.word, l.records FROM mat_lexicon l WHERE l.domain_id = %L '
        'AND l.branch = %L AND l.records > 0%s AND l.word %% $1', p_domain, p_branch, v_kinds);
    IF v_base IS NOT NULL THEN
        v_lookup := v_lookup || ' UNION ALL ' || format(
            'SELECT l.word, l.records FROM mat_lexicon l WHERE l.domain_id = %L '
            'AND l.branch = %L AND l.records > 0%s AND l.word %% $1', p_domain, v_base, v_kinds);
    END IF;
    -- The lexemes of the query as full-text search reads it (tsquery output doubles quotes
    -- and backslashes), but those it negates.
    FOR v_word IN
        SELECT DISTINCT replace(replace(t.m[2], '''''', ''''), '\\\\', '\\')
        FROM regexp_matches(CAST(websearch_to_tsquery(p_config, p_query) AS text),
                            '(!?)''((?:[^'']|'''')*)''', 'g') AS t(m)
        WHERE t.m[1] = ''
        ORDER BY 1
    LOOP
        CONTINUE WHEN char_length(v_word) < p_min_length OR NOT lb_lexicon_word(v_word);
        CONTINUE WHEN EXISTS (
            SELECT 1 FROM mat_lexicon l
            WHERE l.domain_id = p_domain AND l.branch IN (p_branch, v_base)
              AND l.word = v_word AND l.records > 0
              AND (p_kinds IS NULL OR l.kind_id = ANY (p_kinds)));
        RETURN QUERY EXECUTE
            'SELECT CAST($1 AS text), c.word, CAST(max(similarity(c.word, $1)) AS real) '
            'FROM (' || v_lookup || ') AS c GROUP BY c.word '
            'ORDER BY 3 DESC, sum(c.records) DESC, c.word LIMIT $2'
        USING v_word, p_candidates, p_kinds;
    END LOOP;
END
$fn$
"""

LB_SEARCH_CORRECTED = """
CREATE FUNCTION {name}(
    p_domain text, p_branch text, p_kinds text[], p_config regconfig, p_query text,
    p_candidates integer, p_limit integer, p_words text[] DEFAULT NULL,
    p_replacements text[] DEFAULT NULL, p_weight real DEFAULT 1)
RETURNS TABLE (kind_id text, key text, title text, score real, exact boolean)
{{options}}
AS $fn$
DECLARE
    v_kinds text := '';
    v_ts tsquery;
    v_fix tsquery;
    v_word text;
    v_replacements text[];
    v_rows text;
    {{sources}}
BEGIN
    IF cardinality(p_words) IS DISTINCT FROM cardinality(p_replacements) THEN
        RAISE EXCEPTION 'p_words and p_replacements must have the same length'
            USING ERRCODE = '22023';
    END IF;
    IF p_weight IS NULL OR p_weight < 0 THEN
        RAISE EXCEPTION 'p_weight must not be negative' USING ERRCODE = '22023';
    END IF;
    IF p_kinds IS NOT NULL THEN
        v_kinds := ' AND m.kind_id = ANY ($3)';
    END IF;
    v_ts := websearch_to_tsquery(p_config, p_query);
    v_fix := v_ts;
    -- Each corrected lexeme becomes (lexeme | replacement | ...), wherever the query has it.
    FOR v_word, v_replacements IN
        SELECT t.w, array_agg(t.r ORDER BY t.n)
        FROM unnest(p_words, p_replacements) WITH ORDINALITY AS t(w, r, n)
        GROUP BY t.w ORDER BY t.w
    LOOP
        v_fix := ts_rewrite(v_fix, lb_lexeme_query(v_word),
                            lb_lexeme_query(v_word, v_replacements));
    END LOOP;
    v_rows := 'SELECT m.kind_id, m.key, m.title, m.search, %5$s AS exact FROM %1$s m '
              'WHERE m.domain_id = %2$L AND m.branch = %3$L%4$s AND %6$s LIMIT $6';
    IF v_fix = v_ts THEN
        RETURN QUERY EXECUTE format(
            'SELECT c.kind_id, c.key, c.title, CAST(ts_rank_cd(c.search, $8) AS real) AS score, '
            'c.exact FROM (' || v_rows || ') AS c '
            'ORDER BY score DESC, c.kind_id, c.key LIMIT $7',
            v_records, p_domain, p_branch, v_kinds, 'true', 'm.search @@ $8')
        USING p_domain, p_branch, p_kinds, p_config, p_query, p_candidates, p_limit, v_ts;
        RETURN;
    END IF;
    RETURN QUERY EXECUTE format(
        'SELECT c.kind_id, c.key, c.title, CAST(CASE WHEN c.exact '
        'THEN ts_rank_cd(c.search, $8) ELSE $10 * ts_rank_cd(c.search, $9) END AS real) '
        'AS score, c.exact FROM ((' || v_rows || ') UNION ALL ('
        || replace(replace(v_rows, '%5$s', '%7$s'), '%6$s', '%8$s') || ')) AS c '
        'ORDER BY c.exact DESC, score DESC, c.kind_id, c.key LIMIT $7',
        v_records, p_domain, p_branch, v_kinds, 'true', 'm.search @@ $8', 'false',
        'm.search @@ $9 AND NOT m.search @@ $8')
    USING p_domain, p_branch, p_kinds, p_config, p_query, p_candidates, p_limit, v_ts, v_fix,
          p_weight;
END
$fn$
"""

CORRECTIONS = "lb_search_corrections(text, text, text[], regconfig, text, real, integer, integer)"
_CORRECTED_ARGUMENTS = (
    "(text, text, text[], regconfig, text, integer, integer, text[], text[], real)"
)
SEARCH = (
    ("lb_search_corrected", "v_records text := 'mat_records';", ""),
    (
        "lb_search_corrected_overlay",
        "v_records text;",
        "\n    v_records := coalesce(\n"
        "        lb_overlay_rows(p_domain, p_branch, 'mat_records', NULL, false), "
        "'mat_records');\n",
    ),
)
"""(name, declarations, statements run after the checks): the overlay variant reads the
resolved rows (the anti-join hashed, as `lb_search_overlay` since revision 0011)."""
SIGNATURES = (CORRECTIONS, *(f"{name}{_CORRECTED_ARGUMENTS}" for name, _, _ in SEARCH))
HELPERS = ("lb_lexicon_word(text)", "lb_lexeme_query(text, text[])")


def _grant(signatures: Sequence[str]) -> None:
    for signature in signatures:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    role = _reads._application_role()
    if role is not None:
        quoted = op.get_bind().execute(sa.text("SELECT quote_ident(:r)"), {"r": role}).scalar()
        for signature in signatures:
            op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {quoted}")


def _read_functions(search_path: str) -> None:
    options = _reads._OPTIONS.format(search_path=search_path).strip()
    guards = _checks._guards(search_path, ("p_candidates",))
    statement = _checks._create_or_replace(LB_SEARCH_CORRECTIONS, guards)
    op.execute(statement.format(options=options))
    for name, sources, resolve in SEARCH:
        guards = _checks._guards(search_path, ("p_candidates", "p_limit")) + resolve
        template = LB_SEARCH_CORRECTED.format(name=name)
        statement = _checks._create_or_replace(template, guards)
        op.execute(statement.format(options=options, sources=sources))
    _grant(SIGNATURES)


def _lexicon() -> None:
    op.create_table(
        "mat_lexicon",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("kind_id", sa.Text(), nullable=False),
        sa.Column("word", sa.Text(), nullable=False),
        sa.Column("records", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id", "branch"],
            ["refs.domain_id", "refs.name"],
            name=op.f("fk_mat_lexicon_domain_id_branch_refs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "domain_id", "branch", "kind_id", "word", name=op.f("pk_mat_lexicon")
        ),
    )
    op.create_index(
        "ix_mat_lexicon_branch_word",
        "mat_lexicon",
        ["domain_id", "branch", "word"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"word": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_mat_lexicon_spent",
        "mat_lexicon",
        ["domain_id", "branch"],
        unique=False,
        postgresql_where=sa.text("records <= 0"),
    )
    op.execute("ALTER TABLE mat_lexicon ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mat_lexicon FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY domain_isolation ON mat_lexicon "
        f"USING ({DOMAIN_VISIBLE}) WITH CHECK ({DOMAIN_VISIBLE})"
    )


def upgrade() -> None:
    role = _reads._application_role()
    if role is not None:
        _checks._refuse_writable_schemas(role)
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gin")
    search_path = _reads._search_path()
    op.execute(LEXICON_WORD)
    op.execute(LEXEME_QUERY)
    _lexicon()
    op.execute(LEXICON_MAINTAIN.format(search_path=search_path))
    op.execute("REVOKE ALL ON FUNCTION lb_lexicon_maintain() FROM PUBLIC")
    for name, event, tables in TRIGGERS:
        op.execute(
            f"CREATE TRIGGER {name} AFTER {event} ON mat_records REFERENCING {tables} "
            "FOR EACH STATEMENT EXECUTE FUNCTION lb_lexicon_maintain()"
        )
    op.execute("SELECT set_config('lorebase.bypass_rls', 'on', true)")
    op.execute(BACKFILL)
    op.execute("SELECT set_config('lorebase.bypass_rls', '', true)")
    _read_functions(search_path)
    op.execute(f"DROP INDEX IF EXISTS {TITLE_INDEX.name}")


def downgrade() -> None:
    for signature in reversed(SIGNATURES):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    for name, _, _ in reversed(TRIGGERS):
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON mat_records")
    op.execute("DROP FUNCTION IF EXISTS lb_lexicon_maintain()")
    op.drop_table("mat_lexicon")
    for signature in reversed(HELPERS):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    _indexes._build(TITLE_INDEX)
