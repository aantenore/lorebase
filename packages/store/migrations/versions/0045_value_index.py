"""Value index: the distinct short values of selected Attributes per branch (design 8.9).

Resolution (`POST /domains/{id}/resolve`) matches the words of a request against the values
Records hold, so an agent learns that "red" is `Product.color = "Rosso"` before it writes a
query. Scanning `mat_records` for that would read every Record of the Kinds; this revision keeps
the distinct values instead, the way revision 0018 keeps the search lexicon.

- `mat_value_attributes(domain_id, kind_id, attribute_id, max_chars)`: the Attributes whose
  values are indexed, for every branch of the Domain (`lorebase_store.values.sync_selection`
  decides them from the default branch's schema and the `store.values` settings). Values
  longer than `max_chars` characters are left out.
- `mat_values(domain_id, branch, kind_id, attribute_id, value, norm, records)`: per branch,
  each value of a selected Attribute (string scalars and the string elements of arrays) and the
  number of the branch's own live rows holding it. `norm` is `lb_value_norm(value)`: lower
  case (final sigma folded), Latin combining marks dropped after NFD, every blank of
  `BLANKS` collapsed to one space. A btree index on
  `(domain_id, branch, norm)` serves exact lookups, a trigram GIN index (btree_gin) similar
  ones. An overlay holds the values of its own rows, as for the lexicon; readers add its
  base's and count the candidates they return over the resolved rows.
- Statement-level triggers on `mat_records` (`lb_values_maintain`, transition tables) keep
  `mat_values` in step with every write of the commit pipeline, copy-on-write, promotion,
  background copies and builds, snapshots and deletion, in the writing transaction, with the
  lexicon's plan choice for small and large statements. A row is counted per selected
  Attribute whose value changed (an UPDATE that keeps it, or touches other Attributes, adds
  nothing); rows of a branch whose ref is gone (a cascade) leave nothing; spent values (count 0)
  are deleted through a small partial index.
- Fence: each statement first takes the transaction advisory lock of its Domains in shared
  mode (`lb_values_lock_key`), then probes the selection of those Domains (one row of the
  primary key, in its order): a Domain without selected Attributes stops there. A
  selection pass that adds or drops Attributes takes the same lock exclusively, for its
  Domain only: writers of that Domain wait at their next statement, writers of other Domains
  never do, and no write falls between a backfill and the triggers seeing the new selection
  (a writer that wrote before the pass took the lock is waited for; one that writes after it
  reads the new selection). Every writer of `mat_records` goes through the triggers, so no
  writer path needs a lock of its own.
- `lb_values_similar(domain, branch, kinds, terms, threshold, limit)`: for each term, the
  `limit` values of selected Attributes of those Kinds (`NULL`: every Kind) on the branch (and
  its base, for an overlay) whose `norm` is at least `threshold` similar to
  `lb_value_norm(term)`, best first.
  Security definer with the options, checks and grants of revisions 0008 and 0018: trigram
  operators are not leakproof, so under row-level security a plain statement would filter the
  Domain's rows before the index could serve it.
- `mat_value_cursors(domain_id, head, fingerprint, updated_at)`: when the last selection pass
  of a Domain decided (default branch head, schema and settings fingerprint), so a pass with
  the same fingerprint within `store.values.stats_interval_s` only checks sizes.

Revision ID: 0045
Revises: 0044
Create Date: 2026-11-01 10:00:00.000000
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | Sequence[str] | None = "0044"
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

DOMAIN_VISIBLE = _lexicon.DOMAIN_VISIBLE
SMALL_STATEMENT_ROWS = _lexicon.SMALL_STATEMENT_ROWS

COMBINING_MARKS = f"[{chr(0x300)}-{chr(0x36F)}]"
"""Combining diacritical marks: what an accented letter leaves after NFD besides its base."""
BLANKS = "".join(
    chr(c)
    for c in (
        *range(0x09, 0x0E), *range(0x1C, 0x21), 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B),
        0x2028, 0x2029, 0x202F, 0x205F, 0x3000,
    )
)  # fmt: skip
"""Every character Python's `str.split()` takes for white space; `lorebase_store.values` uses
the same set, so both sides collapse the same blanks whatever the database locale."""

VALUE_NORM = f"""
CREATE FUNCTION lb_value_norm(p_value text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN pg_catalog.btrim(pg_catalog.regexp_replace(pg_catalog.regexp_replace(
    pg_catalog.normalize(pg_catalog.translate(pg_catalog.lower(p_value), '{chr(0x3C2)}',
    '{chr(0x3C3)}'), 'NFD'), '{COMBINING_MARKS}', '', 'g'), '[{BLANKS}]+', ' ', 'g'), ' ')
"""
"""The compared form of a value or a word (`lorebase_store.values.normalize` mirrors it).
Qualified names, no SET clause: inlined into the trigger and the read function."""

LOCK_KEY = """
CREATE FUNCTION lb_values_lock_key(p_domain text) RETURNS bigint
LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN pg_catalog.hashtextextended('lorebase.values:' OPERATOR(pg_catalog.||) p_domain, 0)
"""
"""Key of the transaction advisory lock fencing a Domain's value index (module doc)."""

VALUE_STRINGS = """
CREATE FUNCTION lb_value_strings(p_value jsonb, p_max_chars integer) RETURNS SETOF text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $fn$
SELECT DISTINCT s.v FROM (
    SELECT p_value OPERATOR(pg_catalog.#>>) '{}' AS v
    WHERE pg_catalog.jsonb_typeof(p_value) OPERATOR(pg_catalog.=) 'string'
    UNION ALL
    SELECT e.value OPERATOR(pg_catalog.#>>) '{}'
    FROM pg_catalog.jsonb_array_elements(CASE WHEN pg_catalog.jsonb_typeof(p_value)
         OPERATOR(pg_catalog.=) 'array' THEN p_value END) AS e
    WHERE pg_catalog.jsonb_typeof(e.value) OPERATOR(pg_catalog.=) 'string'
) AS s
WHERE s.v OPERATOR(pg_catalog.<>) ''
  AND pg_catalog.char_length(s.v) OPERATOR(pg_catalog.<=) p_max_chars
$fn$
"""
"""The indexed strings of one stored value: the value itself when it is a string, the string
elements of an array, at most `p_max_chars` long, each once."""

_ROWS = """
SELECT {row}.domain_id, {row}.branch, {row}.kind_id, s.attribute_id, v.v AS value,
       {sign} AS records
FROM {table} {row}{live}
JOIN mat_value_attributes s ON s.domain_id = {row}.domain_id AND s.kind_id = {row}.kind_id
CROSS JOIN LATERAL lb_value_strings({row}."values" -> s.attribute_id, s.max_chars) AS v(v)
WHERE NOT {row}.deleted{unchanged}
"""
_LIVE = _lexicon._LIVE
_UNCHANGED = (
    " AND NOT EXISTS (SELECT 1 FROM {other} x WHERE x.domain_id = {row}.domain_id "
    "AND x.branch = {row}.branch AND x.kind_id = {row}.kind_id AND x.key = {row}.key "
    "AND x.deleted = {row}.deleted "
    'AND x."values" -> s.attribute_id IS NOT DISTINCT FROM {row}."values" -> s.attribute_id)'
)
"""An UPDATE that keeps the value of a selected Attribute (titles, search vectors, lineage,
other Attributes) changes nothing for it."""


def _rows(table: str, row: str, sign: int, *, live: bool, other: str | None) -> str:
    return _ROWS.format(
        row=row,
        table=table,
        sign=sign,
        live=_LIVE.format(row=row) if live else "",
        unchanged=_UNCHANGED.format(other=other, row=row) if other else "",
    ).strip()


def _delta(table: str, *arms: str) -> str:
    """The per-value count changes of one statement, into the function's arrays (the plan
    choice of revision 0018's `_delta`)."""
    order = "ORDER BY d.domain_id, d.branch, d.kind_id, d.attribute_id, d.value"
    select = (
        f"SELECT array_agg(d.domain_id {order}), array_agg(d.branch {order}), "
        f"array_agg(d.kind_id {order}), array_agg(d.attribute_id {order}), "
        f"array_agg(d.value {order}), array_agg(d.records {order}) {{into}}"
        "FROM (SELECT x.domain_id, x.branch, x.kind_id, x.attribute_id, x.value, "
        "sum(x.records) AS records FROM ("
        + " UNION ALL ".join(arms)
        + ") AS x GROUP BY x.domain_id, x.branch, x.kind_id, x.attribute_id, x.value "
        "HAVING sum(x.records) <> 0) AS d"
    )
    into = "INTO v_domains, v_branches, v_kind_ids, v_attributes, v_values, v_counts"
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

VALUES_MAINTAIN = f"""
CREATE FUNCTION lb_values_maintain() RETURNS trigger
LANGUAGE plpgsql
SET search_path = {{search_path}}
AS $fn$
DECLARE
    v_domains text[];
    v_branches text[];
    v_kind_ids text[];
    v_attributes text[];
    v_values text[];
    v_counts bigint[];
    v_rows bigint;
    v_scope text[];
    v_domain text;
    v_held boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT array_agg(DISTINCT o.domain_id ORDER BY o.domain_id) INTO v_scope FROM lb_old o;
    ELSE
        SELECT array_agg(DISTINCT n.domain_id ORDER BY n.domain_id) INTO v_scope FROM lb_new n;
    END IF;
    IF v_scope IS NULL THEN
        RETURN NULL;
    END IF;
    -- The fence first, then the selection (module doc). The probe reads the primary key in
    -- its order, one row at most: a plain EXISTS lets the cached generic plan scan the whole
    -- selection of other Domains when one Domain holds most of it.
    FOREACH v_domain IN ARRAY v_scope LOOP
        PERFORM pg_advisory_xact_lock_shared(lb_values_lock_key(v_domain));
        IF v_held IS NULL THEN
            SELECT true INTO v_held FROM mat_value_attributes s WHERE s.domain_id = v_domain
            ORDER BY s.domain_id, s.kind_id, s.attribute_id LIMIT 1;
        END IF;
    END LOOP;
    IF v_held IS NULL THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'INSERT' THEN
        {INSERTED}
    ELSIF TG_OP = 'UPDATE' THEN
        {UPDATED}
    ELSE
        {DELETED}
    END IF;
    IF v_values IS NULL THEN
        RETURN NULL;
    END IF;
    INSERT INTO mat_values AS m (domain_id, branch, kind_id, attribute_id, value, norm, records)
    SELECT d.domain_id, d.branch, d.kind_id, d.attribute_id, d.value, lb_value_norm(d.value),
           d.records
    FROM unnest(v_domains, v_branches, v_kind_ids, v_attributes, v_values, v_counts)
         WITH ORDINALITY AS d(domain_id, branch, kind_id, attribute_id, value, records, n)
    ORDER BY d.n
    ON CONFLICT (domain_id, branch, kind_id, attribute_id, value)
    DO UPDATE SET records = m.records + EXCLUDED.records;
    IF EXISTS (SELECT 1 FROM unnest(v_counts) AS c(records) WHERE c.records < 0) THEN
        DELETE FROM mat_values AS m
        USING (SELECT DISTINCT b.domain_id, b.branch
               FROM unnest(v_domains, v_branches) AS b(domain_id, branch)) AS b
        WHERE m.domain_id = b.domain_id AND m.branch = b.branch AND m.records <= 0;
    END IF;
    RETURN NULL;
END
$fn$
"""

TRIGGERS = (
    ("lb_values_insert", "INSERT", "NEW TABLE AS lb_new"),
    ("lb_values_update", "UPDATE", "OLD TABLE AS lb_old NEW TABLE AS lb_new"),
    ("lb_values_delete", "DELETE", "OLD TABLE AS lb_old"),
)

LB_VALUES_SIMILAR = """
CREATE FUNCTION lb_values_similar(
    p_domain text, p_branch text, p_kinds text[], p_terms text[], p_threshold real,
    p_limit integer)
RETURNS TABLE (term text, kind_id text, attribute_id text, value text, records bigint,
               similarity real)
{options}
SET pg_trgm.similarity_threshold = '0.3'
AS $fn$
DECLARE
    v_base text;
    v_term text;
    v_norm text;
    v_lookup text;
    v_kinds text := '';
BEGIN
    IF p_threshold IS NULL OR NOT (p_threshold > 0 AND p_threshold <= 1) THEN
        RAISE EXCEPTION 'p_threshold must be in (0, 1]' USING ERRCODE = '22023';
    END IF;
    IF cardinality(p_terms) > {max_terms} THEN
        RAISE EXCEPTION 'p_terms must not hold more than {max_terms} terms'
            USING ERRCODE = '22023';
    END IF;
    -- Local to this call: the SET clause above restores the setting on exit.
    PERFORM set_config('pg_trgm.similarity_threshold', p_threshold::text, true);
    SELECT r.base_branch INTO v_base FROM refs r
    WHERE r.domain_id = p_domain AND r.name = p_branch AND r.materialization = 'overlay';
    IF p_kinds IS NOT NULL THEN
        v_kinds := ' AND v.kind_id = ANY ($3)';
    END IF;
    -- Domain and branch are literals: index conditions of the trigram index.
    v_lookup := format(
        'SELECT v.kind_id, v.attribute_id, v.value, v.records, v.norm FROM mat_values v '
        'JOIN mat_value_attributes s ON s.domain_id = v.domain_id AND s.kind_id = v.kind_id '
        'AND s.attribute_id = v.attribute_id '
        'WHERE v.domain_id = %L AND v.branch = %L AND v.records > 0%s AND v.norm %% $1',
        p_domain, p_branch, v_kinds);
    IF v_base IS NOT NULL THEN
        v_lookup := v_lookup || ' UNION ALL ' || format(
            'SELECT v.kind_id, v.attribute_id, v.value, v.records, v.norm FROM mat_values v '
            'JOIN mat_value_attributes s ON s.domain_id = v.domain_id '
            'AND s.kind_id = v.kind_id AND s.attribute_id = v.attribute_id '
            'WHERE v.domain_id = %L AND v.branch = %L AND v.records > 0%s AND v.norm %% $1',
            p_domain, v_base, v_kinds);
    END IF;
    FOREACH v_term IN ARRAY coalesce(p_terms, CAST('{{}}' AS text[])) LOOP
        v_norm := lb_value_norm(v_term);
        CONTINUE WHEN v_norm IS NULL OR v_norm = '';
        RETURN QUERY EXECUTE
            'SELECT CAST($4 AS text), c.kind_id, c.attribute_id, c.value, '
            'CAST(sum(c.records) AS bigint), CAST(max(similarity(c.norm, $1)) AS real) '
            'FROM (' || v_lookup || ') AS c GROUP BY c.kind_id, c.attribute_id, c.value '
            'ORDER BY 6 DESC, 5 DESC, c.value LIMIT $2'
        USING v_norm, p_limit, p_kinds, v_term;
    END LOOP;
END
$fn$
"""

MAX_TERMS = 64
"""Terms one call of `lb_values_similar` compares (`lorebase_store.values.MAX_TERMS`)."""
SIMILAR = "lb_values_similar(text, text, text[], text[], real, integer)"
HELPERS = ("lb_value_norm(text)", "lb_value_strings(jsonb, integer)", "lb_values_lock_key(text)")


def _domain_table(name: str, *columns: sa.Column[object], key: Sequence[str]) -> None:
    op.create_table(
        name,
        sa.Column("domain_id", sa.Text(), nullable=False),
        *columns,
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f(f"fk_{name}_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", *key, name=op.f(f"pk_{name}")),
    )
    _isolate(name)


def _isolate(name: str) -> None:
    op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY domain_isolation ON {name} "
        f"USING ({DOMAIN_VISIBLE}) WITH CHECK ({DOMAIN_VISIBLE})"
    )


def _tables() -> None:
    _domain_table(
        "mat_value_attributes",
        sa.Column("kind_id", sa.Text(), nullable=False),
        sa.Column("attribute_id", sa.Text(), nullable=False),
        sa.Column("max_chars", sa.Integer(), nullable=False),
        key=("kind_id", "attribute_id"),
    )
    _domain_table(
        "mat_value_cursors",
        sa.Column("head", sa.LargeBinary(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        key=(),
    )
    op.create_table(
        "mat_values",
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("kind_id", sa.Text(), nullable=False),
        sa.Column("attribute_id", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("norm", sa.Text(), nullable=False),
        sa.Column("records", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id", "branch"],
            ["refs.domain_id", "refs.name"],
            name=op.f("fk_mat_values_domain_id_branch_refs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "domain_id", "branch", "kind_id", "attribute_id", "value", name=op.f("pk_mat_values")
        ),
    )
    op.create_index("ix_mat_values_norm", "mat_values", ["domain_id", "branch", "norm"])
    op.create_index(
        "ix_mat_values_trgm",
        "mat_values",
        ["domain_id", "branch", "norm"],
        postgresql_using="gin",
        postgresql_ops={"norm": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_mat_values_spent",
        "mat_values",
        ["domain_id", "branch"],
        postgresql_where=sa.text("records <= 0"),
    )
    _isolate("mat_values")


def _read_function(search_path: str) -> None:
    options = _reads._OPTIONS.format(search_path=search_path).strip()
    guards = _checks._guards(search_path, ("p_limit",))
    statement = _checks._create_or_replace(LB_VALUES_SIMILAR, guards)
    op.execute(statement.format(options=options, max_terms=MAX_TERMS))
    _lexicon._grant((SIMILAR,))


def upgrade() -> None:
    role = _reads._application_role()
    if role is not None:
        _checks._refuse_writable_schemas(role)
    search_path = _reads._search_path()
    op.execute(VALUE_NORM)
    op.execute(VALUE_STRINGS)
    op.execute(LOCK_KEY)
    _tables()
    op.execute(VALUES_MAINTAIN.format(search_path=search_path))
    op.execute("REVOKE ALL ON FUNCTION lb_values_maintain() FROM PUBLIC")
    for name, event, tables in TRIGGERS:
        op.execute(
            f"CREATE TRIGGER {name} AFTER {event} ON mat_records REFERENCING {tables} "
            "FOR EACH STATEMENT EXECUTE FUNCTION lb_values_maintain()"
        )
    _read_function(search_path)


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {SIMILAR}")
    for name, _, _ in reversed(TRIGGERS):
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON mat_records")
    op.execute("DROP FUNCTION IF EXISTS lb_values_maintain()")
    for table in ("mat_values", "mat_value_cursors", "mat_value_attributes"):
        op.drop_table(table)
    for signature in reversed(HELPERS):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
