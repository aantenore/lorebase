"""Shared lineage maps and storage profile state (store schema notes, docs/performance.md).

A Record row of `mat_records` held two maps per Attribute, `lineage` (Attribute -> provenance
id) and `lineage_revisions` (Attribute -> Revision), and a Link row of `mat_links` its
`lineage`: about half of a Record row, and the same few hundred distinct maps repeated over
millions of rows. With shared maps a row holds `{"$map": "<hex id>"}` instead, and the map is
stored once per Domain in `mat_lineage_maps`, keyed by the first 16 bytes of the SHA-256 of
its canonical text.

- `mat_lineage_maps(domain_id, id, map)`: Domain-scoped with row-level security; rows go with
  their Domain. Maps are immutable and never updated; a map no row names any more stays.
- `lb_lineage(domain, value)`: the map a stored value stands for (the value itself when it
  is not a reference). Every reader of the columns goes through it; a reference whose map is
  missing raises an error instead of reading as empty.
- `lb_lineage_share(domain, map)`: stores a map and returns its reference; maps smaller than
  a reference, references and non-objects come back unchanged.
- Row triggers `lb_lineage_maps` (BEFORE INSERT or UPDATE of the map columns) on `mat_records`
  and `mat_links` replace maps by references for every writer (commit pipeline, copies,
  copy-on-write, rebuilds, backfills); a value that already is a reference passes through.
  The storage profile (`store.storage.lineage_maps`) enables them or not; `python -m
  lorebase_store.storage apply` switches them and rewrites the existing rows in batches, in
  either direction.
- `storage_tasks(name, state)`: progress of the resumable steps of `storage apply`.

The triggers start enabled only on a database without materialized Records (a new install):
on an existing one, processes of the previous version may still run during a rolling upgrade
and read the columns as maps, so they stay disabled until `storage apply` runs once every
process has the code of this revision. Existing rows keep their maps until then; readers of
this revision accept both forms. The
downgrade writes every map back into its rows (one statement per table) before dropping the
objects.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-23 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0036"
down_revision: str | Sequence[str] | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAPS = "mat_lineage_maps"
TASKS = "storage_tasks"
TRIGGER = "lb_lineage_maps"
GRANTED = "SELECT, INSERT, UPDATE, DELETE"
REVOKED = "TRUNCATE, REFERENCES, TRIGGER"

DOMAIN_VISIBLE = (
    "current_setting('lorebase.bypass_rls', true) = 'on' "
    "OR domain_id = ANY (string_to_array(current_setting('lorebase.domain_ids', true), ','))"
)

MAP_KEY = "$map"
"""Key of a reference; Attribute ids and `$created` never take this form."""

MIN_SHARED_BYTES = 64
"""Maps of at most this many bytes stay in their row: a reference is about as large."""

LINEAGE_MISSING = """
CREATE FUNCTION lb_lineage_missing(p_domain text, p_id text) RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL SAFE AS $fn$
BEGIN
    RAISE EXCEPTION 'lineage map % of Domain % is missing', p_id, p_domain
        USING ERRCODE = 'XX001';
END
$fn$
"""

LINEAGE = f"""
CREATE FUNCTION lb_lineage(p_domain text, p_value jsonb) RETURNS jsonb
LANGUAGE sql STABLE PARALLEL SAFE AS $fn$
    SELECT CASE
        WHEN jsonb_typeof(p_value) = 'object' AND p_value ? '{MAP_KEY}' THEN coalesce(
            (SELECT m.map FROM __MAPS__ m
             WHERE m.domain_id = p_domain AND m.id = decode(p_value ->> '{MAP_KEY}', 'hex')),
            lb_lineage_missing(p_domain, p_value ->> '{MAP_KEY}'))
        ELSE p_value
    END
$fn$
"""

LINEAGE_SHARE = f"""
CREATE FUNCTION lb_lineage_share(p_domain text, p_map jsonb) RETURNS jsonb
LANGUAGE plpgsql VOLATILE AS $fn$
DECLARE
    v_id bytea;
BEGIN
    IF p_map IS NULL OR jsonb_typeof(p_map) <> 'object' OR p_map ? '{MAP_KEY}'
       OR pg_column_size(p_map) <= {MIN_SHARED_BYTES} THEN
        RETURN p_map;
    END IF;
    v_id := substr(sha256(convert_to(p_map::text, 'UTF8')), 1, 16);
    PERFORM 1 FROM __MAPS__ WHERE domain_id = p_domain AND id = v_id;
    IF NOT FOUND THEN
        INSERT INTO __MAPS__ (domain_id, id, map) VALUES (p_domain, v_id, p_map)
        ON CONFLICT DO NOTHING;
    END IF;
    RETURN jsonb_build_object('{MAP_KEY}', encode(v_id, 'hex'));
END
$fn$
"""

RECORD_TRIGGER = """
CREATE FUNCTION lb_lineage_maps_records() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    NEW.lineage := lb_lineage_share(NEW.domain_id, NEW.lineage);
    NEW.lineage_revisions := lb_lineage_share(NEW.domain_id, NEW.lineage_revisions);
    RETURN NEW;
END
$fn$
"""

LINK_TRIGGER = """
CREATE FUNCTION lb_lineage_maps_links() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    NEW.lineage := lb_lineage_share(NEW.domain_id, NEW.lineage);
    RETURN NEW;
END
$fn$
"""

TABLES = (
    ("mat_records", "lb_lineage_maps_records", ("lineage", "lineage_revisions")),
    ("mat_links", "lb_lineage_maps_links", ("lineage",)),
)
"""(table, trigger function, map columns)."""

TRIGGER_FUNCTIONS = ("lb_lineage_maps_records()", "lb_lineage_maps_links()")
FUNCTIONS = (
    *TRIGGER_FUNCTIONS,
    "lb_lineage_share(text, jsonb)",
    "lb_lineage(text, jsonb)",
    "lb_lineage_missing(text, text)",
)


def _attribute(name: str) -> object:
    config = op.get_context().config
    if config is None or name not in config.attributes:
        raise RuntimeError("run this migration through the Lorebase Alembic environment")
    return config.attributes[name]


def _application_role() -> str | None:
    role: str | None = _attribute("database_settings").application_role  # type: ignore[attr-defined]
    return role


def _shared() -> bool:
    """Whether the triggers start enabled: on a database without materialized Records (a new
    install, where no process of an earlier version can read the references) when the profile
    shares lineage maps (`store.storage.lineage_maps`, else every profile but `fast`, as
    `lorebase_store.storage.PROFILES` when this was written). An existing install keeps its
    maps inline until `storage apply` runs, once every process reads references."""
    storage = _attribute("store_settings").storage  # type: ignore[attr-defined]
    if storage.lineage_maps is not None:
        wanted = bool(storage.lineage_maps == "shared")
    else:
        wanted = bool(storage.profile != "fast")
    if not wanted:
        return False
    op.execute("SELECT set_config('lorebase.bypass_rls', 'on', true)")
    found = op.get_bind().execute(sa.text("SELECT 1 FROM mat_records LIMIT 1")).first()
    op.execute("SELECT set_config('lorebase.bypass_rls', '', true)")
    return found is None


def _maps_name() -> str:
    """`mat_lineage_maps` qualified with the schema the migration runs in: the functions run
    with the caller's search_path (`lb_lineage` must stay inlinable, so it has no SET)."""
    name: str = (
        op.get_bind()
        .execute(
            sa.text("SELECT quote_ident(current_schema()) || '.' || quote_ident(:t)"), {"t": MAPS}
        )
        .scalar_one()
    )
    return name


def _quoted(role: str) -> str:
    quoted: str = op.get_bind().execute(sa.text("SELECT quote_ident(:r)"), {"r": role}).scalar_one()
    return quoted


def _grant(table: str) -> None:
    op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
    role = _application_role()
    if role is not None:
        quoted = _quoted(role)
        op.execute(f"REVOKE {REVOKED} ON TABLE {table} FROM {quoted}")
        op.execute(f"GRANT {GRANTED} ON TABLE {table} TO {quoted}")


def upgrade() -> None:
    op.create_table(
        MAPS,
        sa.Column("domain_id", sa.Text(), nullable=False),
        sa.Column("id", sa.LargeBinary(), nullable=False),
        sa.Column("map", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("octet_length(id) = 16", name=op.f("ck_mat_lineage_maps_id_h16")),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_mat_lineage_maps_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("domain_id", "id", name=op.f("pk_mat_lineage_maps")),
    )
    op.execute(f"ALTER TABLE {MAPS} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {MAPS} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY domain_isolation ON {MAPS} "
        f"USING ({DOMAIN_VISIBLE}) WITH CHECK ({DOMAIN_VISIBLE})"
    )
    _grant(MAPS)
    op.create_table(
        TASKS,
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "state",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_storage_tasks")),
    )
    _grant(TASKS)
    maps = _maps_name()
    for statement in (LINEAGE_MISSING, LINEAGE, LINEAGE_SHARE, RECORD_TRIGGER, LINK_TRIGGER):
        op.execute(statement.replace("__MAPS__", maps))
    # Trigger functions are not called directly. The others run as their caller (no SECURITY
    # DEFINER), under its table privileges and row-level security, so every role may call them.
    for signature in TRIGGER_FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    shared = _shared()
    for table, function, columns in TABLES:
        op.execute(
            f"CREATE TRIGGER {TRIGGER} BEFORE INSERT OR UPDATE OF {', '.join(columns)} "
            f"ON {table} FOR EACH ROW EXECUTE FUNCTION {function}()"
        )
        if not shared:
            op.execute(f"ALTER TABLE {table} DISABLE TRIGGER {TRIGGER}")


def downgrade() -> None:
    op.execute("SELECT set_config('lorebase.bypass_rls', 'on', true)")
    bind = op.get_bind()
    for table, _, columns in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON {table}")
        # Lock out writers that would copy references meanwhile (reads go on).
        op.execute(f"LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE")
        assignments = ", ".join(f"{c} = lb_lineage(domain_id, {c})" for c in columns)
        references = " OR ".join(
            f"(jsonb_typeof({c}) = 'object' AND {c} ? '{MAP_KEY}')" for c in columns
        )
        # Partition by partition: the statement-level triggers of the parent (lexicon) do not
        # run for these rows, whose words do not change.
        partitions = bind.execute(
            sa.text(
                "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                "WHERE i.inhparent = CAST(:t AS regclass) ORDER BY c.relname"
            ),
            {"t": table},
        ).scalars().all() or [table]
        for partition in partitions:
            op.execute(f"UPDATE {partition} SET {assignments} WHERE {references}")
        left = bind.execute(sa.text(f"SELECT 1 FROM {table} WHERE {references} LIMIT 1")).first()
        if left is not None:
            raise RuntimeError(f"{table} still holds lineage map references; not dropping maps")
    op.execute("SELECT set_config('lorebase.bypass_rls', '', true)")
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.drop_table(TASKS)
    op.drop_table(MAPS)
