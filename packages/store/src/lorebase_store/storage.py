"""Storage profiles: what the database keeps, traded against memory, disk and speed.

`store.storage.profile` (`compact`, `balanced`, `fast`) picks each choice below; every other
key of `store.storage` overrides one (docs/performance.md#storage-profiles):

| choice | compact | balanced | fast |
|---|---|---|---|
| `lineage_maps`: lineage maps of Records and Links | shared | shared | inline |
| `values_index`: GIN index on Record values | off | on | on |
| `attribute_indexes`: per-Attribute indexes built | constraints | all | all |
| `lexicon_index`: trigram index of the word lexicon | off | on | on |
| `embedding_precision`: HNSW index of embeddings | half | full | full |
| `compression`: TOAST compression of large values | pglz | lz4 | lz4 |
| `records_fillfactor` | 100 | 100 | 90 |
| `prewarm`: load the hot relations after a restart | off | off | on |

Every feature works with every profile; what a profile leaves out makes some reads slower:
equality filters on Attributes without an index scan the Kind, typo corrections compare
every word of the searched Kinds, an inline lineage map makes rows twice as large.

The database keeps what it has until `python -m lorebase_store.storage apply` runs: reads
accept both forms of a lineage map, use whichever HNSW index exists, and work without the
optional indexes. `apply` is batched and resumable (`storage_tasks`); reads go on during it:

- lineage maps: enables or disables the `lb_lineage_maps` triggers (revision 0036), then
  rewrites the rows of `mat_records` and `mat_links` page range by page range;
- indexes: built with `CREATE INDEX CONCURRENTLY`, dropped `CONCURRENTLY` where Postgres
  allows it. The values index is a partitioned index: its partitions are built concurrently
  and attached; dropping it takes a short exclusive lock (waits at most
  `store.storage.lock_timeout_ms`, then the step is retried by the next run);
- per-Attribute indexes: queued as index jobs (`python -m lorebase_store.jobs work`);
- compression and fill factor: catalog changes that apply to rows written from then on;
- `--compact`: moves the rows of the last pages of `mat_records`, `mat_links` and
  `mat_history` into the free space of earlier pages (one branch at a time, under the branch
  lock that commits take: every writer of these tables holds it, see the store schema
  notes), lets VACUUM give the emptied pages back, and with `--reindex`
  rebuilds their indexes concurrently. This is what shrinks a table after a rewrite.

`prewarm` loads the hot relations of the agent read path, most valuable first, into shared
buffers with `pg_prewarm` (up to `store.storage.prewarm_fraction` of `shared_buffers`);
`autoprewarm` (`shared_preload_libraries = 'pg_prewarm'`) restores them after a restart.
`status` prints the choices, what the database has, and the size of the hot relations.

DDL needs the role that owns the tables: the maintenance role when configured
(`database.maintenance_dsn`), else the configured role.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from lorebase_store.repositories.refs import advisory_lock_key, sqlstate
from lorebase_store.settings import StorageSettings, StoreSettings

log = logging.getLogger(__name__)

LineageMaps = Literal["shared", "inline"]
AttributeIndexes = Literal["all", "constraints"]
Precision = Literal["half", "full"]
Compression = Literal["lz4", "pglz"]

LINEAGE_TRIGGER = "lb_lineage_maps"
"""Row trigger of revision 0036 on `mat_records` and `mat_links`."""
MAP_KEY = "$map"
"""Key of a reference to a shared lineage map."""
VALUES_INDEX = "ix_mat_records_values"
LEXICON_INDEX = "ix_mat_lexicon_branch_word"
PRECISION_TTL_S = 60.0
"""How long a process trusts the HNSW precision it found (`embeddings.index_precision`)."""
HALFVEC_MIN_VERSION = (0, 7, 0)
"""First pgvector release with `halfvec`."""
LOCK_NOT_AVAILABLE = "55P03"
DEADLOCK = "40P01"

LINEAGE_TABLES: Mapping[str, tuple[str, ...]] = {
    "mat_records": ("lineage", "lineage_revisions"),
    "mat_links": ("lineage",),
}
"""Tables with lineage map columns."""
COMPRESSED: Mapping[str, tuple[str, ...]] = {
    "mat_records": ("values", "derived", "lineage", "lineage_revisions", "search"),
    "mat_links": ("values", "lineage"),
    "mat_history": ("provenance",),
}
"""Columns whose large values are compressed (TOAST) with the profile's method."""
COMPACTED = ("mat_records", "mat_links", "mat_history")
"""Tables `--compact` moves rows of; every row belongs to one branch."""


@dataclass(frozen=True, slots=True)
class StorageChoices:
    """What a profile keeps (see the module documentation)."""

    lineage_maps: LineageMaps
    values_index: bool
    attribute_indexes: AttributeIndexes
    lexicon_index: bool
    embedding_precision: Precision
    compression: Compression
    records_fillfactor: int
    prewarm: bool


PROFILES: Mapping[str, StorageChoices] = {
    "compact": StorageChoices(
        lineage_maps="shared",
        values_index=False,
        attribute_indexes="constraints",
        lexicon_index=False,
        embedding_precision="half",
        compression="pglz",
        records_fillfactor=100,
        prewarm=False,
    ),
    "balanced": StorageChoices(
        lineage_maps="shared",
        values_index=True,
        attribute_indexes="all",
        lexicon_index=True,
        embedding_precision="full",
        compression="lz4",
        records_fillfactor=100,
        prewarm=False,
    ),
    "fast": StorageChoices(
        lineage_maps="inline",
        values_index=True,
        attribute_indexes="all",
        lexicon_index=True,
        embedding_precision="full",
        compression="lz4",
        records_fillfactor=90,
        prewarm=True,
    ),
}


def storage_choices(settings: StorageSettings) -> StorageChoices:
    """The profile's choices with the keys set in `settings` instead."""
    choices = PROFILES[settings.profile]
    overrides = {
        name: getattr(settings, name)
        for name in StorageChoices.__dataclass_fields__
        if getattr(settings, name, None) is not None
    }
    return replace(choices, **overrides)


def constraints_only(settings: StoreSettings) -> bool:
    """Whether per-Attribute indexes are built for unique and foreign key Attributes only."""
    return storage_choices(settings.storage).attribute_indexes == "constraints"


# What the database has ------------------------------------------------------------------------


@dataclass(slots=True)
class StorageState:
    """The physical state the choices are compared with."""

    lineage_triggers: dict[str, bool] = field(default_factory=dict)
    values_index: bool = False
    values_index_partitions: list[str] = field(default_factory=list)
    lexicon_index: bool = False
    hnsw: dict[str, bool] = field(default_factory=dict)
    invalid_indexes: list[str] = field(default_factory=list)
    """Optional indexes that exist but are not valid (an interrupted concurrent build, a
    partitioned index with partitions not attached yet): the flags above count only valid ones."""
    compression: dict[str, str] = field(default_factory=dict)
    records_fillfactor: int = 100
    pgvector: tuple[int, ...] | None = None
    prewarm_extension: bool = False
    prewarm_available: bool = False
    autoprewarm: bool = False
    lineage_rows: dict[str, dict[str, int]] = field(default_factory=dict)
    """Per table, rows of a 0.1% page sample holding a reference (`shared`) and holding
    an inline map larger than a reference (`inline`)."""


def hnsw_index_name(dims: int, precision: Precision) -> str:
    """Name of the HNSW index of `embeddings` for `dims` (`half`: on `halfvec`)."""
    return f"embeddings_hnsw_{dims}" if precision == "full" else f"embeddings_hnsw_half_{dims}"


def hnsw_index_sql(dims: int, precision: Precision, *, concurrently: bool = True) -> str:
    """The HNSW index of `embeddings` for `dims` (as revision 0001 creates the full one)."""
    kind = "vector" if precision == "full" else "halfvec"
    ops = f"{kind}_cosine_ops"
    how = "CONCURRENTLY " if concurrently else ""
    return (
        f"CREATE INDEX {how}IF NOT EXISTS {hnsw_index_name(dims, precision)} ON embeddings "
        f"USING hnsw ((embedding::{kind}({dims})) {ops}) WHERE dims = {dims}"
    )


def _version(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    parts: list[int] = []
    for piece in value.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


async def _partitions(connection: AsyncConnection, table: str) -> list[str]:
    """`table` itself, or its partitions when it is partitioned."""
    rows = await connection.execute(
        text(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = CAST(:t AS regclass) ORDER BY c.relname"
        ),
        {"t": table},
    )
    found = [str(name) for name in rows.scalars()]
    return found or [table]


async def read_state(connection: AsyncConnection, dims: int) -> StorageState:
    """What the database has now."""
    state = StorageState()
    for table in LINEAGE_TABLES:
        enabled = await connection.scalar(
            text(
                "SELECT tgenabled <> 'D' FROM pg_trigger "
                "WHERE tgrelid = CAST(:t AS regclass) AND tgname = :n"
            ),
            {"t": table, "n": LINEAGE_TRIGGER},
        )
        state.lineage_triggers[table] = bool(enabled)
    found = await connection.execute(
        text(
            "SELECT c.relname, x.indisvalid FROM pg_index x "
            "JOIN pg_class c ON c.oid = x.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = current_schema()"
        )
    )
    valid = {str(name): bool(ok) for name, ok in found}
    names = set(valid)
    optional = [VALUES_INDEX, LEXICON_INDEX, *(hnsw_index_name(dims, p) for p in ("full", "half"))]
    state.invalid_indexes = [name for name in optional if name in valid and not valid[name]]
    state.values_index = valid.get(VALUES_INDEX, False)
    state.lexicon_index = valid.get(LEXICON_INDEX, False)
    for precision in ("full", "half"):
        state.hnsw[precision] = valid.get(hnsw_index_name(dims, precision), False)
    state.values_index_partitions = [
        p for p in await _partitions(connection, "mat_records") if f"{p}_values_idx" in names
    ]
    rows = await connection.execute(
        text(
            "SELECT c.relname || '.' || a.attname, CAST(a.attcompression AS text) "
            "FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "WHERE c.relname ~ CAST(:tables AS text) AND a.attname = ANY(CAST(:cols AS text[])) "
            "AND c.relkind IN ('r', 'p')"
        ),
        {
            "tables": "^(" + "|".join(COMPRESSED) + ")(_p[0-9]+)?$",
            "cols": sorted({c for cols in COMPRESSED.values() for c in cols}),
        },
    )
    method = {"p": "pglz", "l": "lz4", "": "default"}
    state.compression = {str(name): method.get(str(code), str(code)) for name, code in rows}
    # Set on the partitions (a partitioned table has no storage parameters); mixed reads as 0.
    factors = set()
    for partition in await _partitions(connection, "mat_records"):
        options = await connection.scalar(
            text("SELECT reloptions FROM pg_class WHERE oid = CAST(:t AS regclass)"),
            {"t": partition},
        )
        given = [str(o).split("=", 1)[1] for o in options or () if str(o).startswith("fillfactor=")]
        factors.add(int(given[0]) if given else 100)
    state.records_fillfactor = factors.pop() if len(factors) == 1 else 0
    state.pgvector = _version(
        await connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
    )
    state.prewarm_extension = bool(
        await connection.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'pg_prewarm'"))
    )
    state.prewarm_available = bool(
        await connection.scalar(
            text("SELECT 1 FROM pg_available_extensions WHERE name = 'pg_prewarm'")
        )
    )
    preload = str(await connection.scalar(text("SHOW shared_preload_libraries")) or "")
    state.autoprewarm = "pg_prewarm" in {p.strip() for p in preload.split(",")}
    for table, columns in LINEAGE_TABLES.items():
        shared = _map_columns_condition(columns, shared=False)
        inline = _map_columns_condition(columns, shared=True)
        row = (
            await connection.execute(
                text(
                    f"SELECT count(*) FILTER (WHERE {shared}), "
                    f"count(*) FILTER (WHERE {inline}) FROM {table} TABLESAMPLE SYSTEM (0.1)"
                )
            )
        ).one()
        state.lineage_rows[table] = {"shared": int(row[0]), "inline": int(row[1])}
    return state


# Reports ------------------------------------------------------------------------------------------


@dataclass(slots=True)
class StepResult:
    step: str
    status: Literal["done", "skipped", "deferred", "failed", "planned"]
    detail: str = ""
    rows: int = 0


@dataclass(slots=True)
class ApplyReport:
    profile: str
    choices: dict[str, Any]
    steps: list[StepResult] = field(default_factory=list)

    def add(self, step: str, status: Any, detail: str = "", rows: int = 0) -> None:
        self.steps.append(StepResult(step, status, detail, rows))
        log.info("storage %s: %s %s", step, status, detail)

    @property
    def complete(self) -> bool:
        return all(s.status in ("done", "skipped") for s in self.steps)

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "choices": self.choices,
            "complete": self.complete,
            "steps": [asdict(s) for s in self.steps],
        }


# Apply --------------------------------------------------------------------------------------------


class _LockedError(Exception):
    """A lock was not granted within `lock_timeout_ms`."""


class _NoRoomError(Exception):
    """Rows moved out of the last pages landed in them again (rolled back)."""


async def _autocommit(engine: AsyncEngine) -> AsyncConnection:
    connection = await engine.connect()
    connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
    await connection.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', false)"))
    return connection


async def _ddl(connection: AsyncConnection, statement: str, lock_timeout_ms: int) -> None:
    """Run DDL waiting at most `lock_timeout_ms` for its locks."""
    await connection.exec_driver_sql(f"SET lock_timeout = '{int(lock_timeout_ms)}ms'")
    try:
        await connection.exec_driver_sql(statement)
    except DBAPIError as error:
        if sqlstate(error) in (LOCK_NOT_AVAILABLE, DEADLOCK):
            raise _LockedError(statement) from error
        raise
    finally:
        await connection.exec_driver_sql("SET lock_timeout = 0")


async def _task(connection: AsyncConnection, name: str) -> dict[str, Any]:
    state = await connection.scalar(
        text("SELECT state FROM storage_tasks WHERE name = :n"), {"n": name}
    )
    if state is None:
        return {}
    return dict(json.loads(state) if isinstance(state, str) else state)


async def _save_task(connection: AsyncConnection, name: str, state: Mapping[str, Any]) -> None:
    await connection.execute(
        text(
            "INSERT INTO storage_tasks (name, state, updated_at) "
            "VALUES (:n, CAST(:s AS jsonb), now()) "
            "ON CONFLICT (name) DO UPDATE SET state = excluded.state, updated_at = now()"
        ),
        {"n": name, "s": json.dumps(dict(state))},
    )


async def _pages(connection: AsyncConnection, table: str) -> int:
    size = await connection.scalar(
        text("SELECT pg_relation_size(CAST(:t AS regclass))"), {"t": table}
    )
    return int(size or 0) // 8192


async def _rows_per_page(connection: AsyncConnection, table: str) -> float:
    row = (
        await connection.execute(
            text("SELECT reltuples, relpages FROM pg_class WHERE oid = CAST(:t AS regclass)"),
            {"t": table},
        )
    ).one()
    tuples, pages = float(row[0] or 0), float(row[1] or 0)
    return tuples / pages if tuples > 0 and pages > 0 else 4.0


def _map_columns_condition(columns: Sequence[str], *, shared: bool) -> str:
    """Rows of the batch the lineage rewrite changes."""
    if shared:
        one = (
            "(jsonb_typeof({c}) = 'object' AND NOT {c} ? '" + MAP_KEY + "' "
            "AND pg_column_size({c}) > 64)"
        )
    else:
        one = "(jsonb_typeof({c}) = 'object' AND {c} ? '" + MAP_KEY + "')"
    return " OR ".join(one.format(c=c) for c in columns)


async def _rewrite_lineage(
    engine: AsyncEngine,
    settings: StorageSettings,
    shared: bool,
    report: ApplyReport,
    *,
    max_batches: int | None,
    leftovers: Mapping[str, Mapping[str, int]] | None = None,
) -> None:
    """Rewrite the map columns of every row, page range by page range, from the first page
    to the last page the table had when the pass started (rows a concurrent write puts after
    it are written by the trigger, or are already inline). Resumes where it stopped."""
    mode = "shared" if shared else "inline"
    async with engine.connect() as probe:
        work = [
            (table, partition)
            for table in LINEAGE_TABLES
            for partition in await _partitions(probe, table)
        ]
    batches = 0
    for table, partition in work:
        columns = LINEAGE_TABLES[table]
        name = f"lineage:{partition}"
        # Rows of the other form found after a finished pass (copies of rows written before
        # the triggers switched) start the pass again.
        stale = bool((leftovers or {}).get(table, {}).get("shared" if not shared else "inline"))
        async with engine.begin() as connection:
            state = await _task(connection, name)
            if state.get("mode") == mode and state.get("done") and not stale:
                continue
            if state.get("mode") != mode or state.get("done"):
                state = {
                    "mode": mode,
                    "page": 0,
                    "end": await _pages(connection, partition),
                    "rows": 0,
                    "done": False,
                }
                await _save_task(connection, name, state)
            per_page = await _rows_per_page(connection, partition)
        step = max(1, int(settings.batch_rows / max(per_page, 1.0)))
        if shared:
            assignments = ", ".join(f"{c} = {c}" for c in columns)
        else:
            assignments = ", ".join(f"{c} = lb_lineage(domain_id, {c})" for c in columns)
        condition = _map_columns_condition(columns, shared=shared)
        while int(state["page"]) < int(state["end"]):
            if max_batches is not None and batches >= max_batches:
                report.add(f"lineage {partition}", "deferred", f"stopped at page {state['page']}")
                return
            start, stop = int(state["page"]), min(int(state["page"]) + step, int(state["end"]))
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        text("SELECT set_config('lorebase.bypass_rls', 'on', true)")
                    )
                    await connection.exec_driver_sql(
                        f"SET LOCAL lock_timeout = '{int(settings.lock_timeout_ms)}ms'"
                    )
                    # Rows a commit holds are skipped, never waited for: the rewrite cannot
                    # deadlock with commits, which lock rows in key order. A later pass picks
                    # the skipped rows up (`leftovers`).
                    result = await connection.execute(
                        text(
                            f"UPDATE {partition} SET {assignments} WHERE ctid = ANY(ARRAY("
                            f"SELECT ctid FROM {partition} "
                            f"WHERE ctid >= CAST(:lo AS tid) AND ctid < CAST(:hi AS tid) "
                            f"AND ({condition}) FOR UPDATE SKIP LOCKED))"
                        ),
                        {"lo": (start, 0), "hi": (stop, 0)},
                    )
                    state = {
                        **state,
                        "page": stop,
                        "rows": int(state["rows"]) + int(result.rowcount or 0),
                    }
                    if stop >= int(state["end"]):
                        state["done"] = True
                    await _save_task(connection, name, state)
            except DBAPIError as error:
                if sqlstate(error) not in (LOCK_NOT_AVAILABLE, DEADLOCK):
                    raise
                report.add(
                    f"lineage {partition}", "deferred", f"lock conflict at page {start}; run again"
                )
                return
            batches += 1
        if not state.get("done"):  # an empty partition
            async with engine.begin() as connection:
                await _save_task(connection, name, {**state, "done": True})
        if int(state["rows"]) == 0:
            continue
        report.add(
            f"lineage {partition}", "done", f"{mode}, {state['rows']} rows", int(state["rows"])
        )


async def _switch_triggers(
    ddl: AsyncConnection,
    state: StorageState,
    shared: bool,
    settings: StorageSettings,
    report: ApplyReport,
) -> bool:
    """Enable or disable the lineage map triggers; `False` when a lock was not granted."""
    ok = True
    for table in LINEAGE_TABLES:
        if state.lineage_triggers.get(table) == shared:
            continue
        action = "ENABLE" if shared else "DISABLE"
        try:
            await _ddl(
                ddl,
                f"ALTER TABLE {table} {action} TRIGGER {LINEAGE_TRIGGER}",
                settings.lock_timeout_ms,
            )
            report.add(f"lineage trigger {table}", "done", action.lower())
        except _LockedError:
            ok = False
            report.add(f"lineage trigger {table}", "deferred", "lock not granted; run again")
    return ok


async def _values_index(
    ddl: AsyncConnection,
    state: StorageState,
    wanted: bool,
    settings: StorageSettings,
    report: ApplyReport,
) -> None:
    if wanted and state.values_index:
        return
    if not wanted:
        if not state.values_index and VALUES_INDEX not in state.invalid_indexes:
            # Leftovers of an interrupted build.
            for partition in state.values_index_partitions:
                await ddl.exec_driver_sql(
                    f"DROP INDEX CONCURRENTLY IF EXISTS {partition}_values_idx"
                )
            return
        try:
            await _ddl(ddl, f"DROP INDEX IF EXISTS {VALUES_INDEX}", settings.lock_timeout_ms)
            report.add("values index", "done", "dropped")
        except _LockedError:
            report.add("values index", "deferred", "lock not granted; run again")
        return
    # Built partition by partition without blocking writes, then attached to the parent.
    partitions = await _partitions(ddl, "mat_records")
    for partition in partitions:
        index = f"{partition}_values_idx"
        valid = await ddl.scalar(
            text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:i)"),
            {"i": index},
        )
        if valid is False:
            await ddl.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {index}")
        await ddl.exec_driver_sql(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index} ON {partition} "
            f'USING gin ("values" jsonb_path_ops)'
        )
    try:
        await _ddl(
            ddl,
            f"CREATE INDEX IF NOT EXISTS {VALUES_INDEX} ON ONLY mat_records "
            f'USING gin ("values" jsonb_path_ops)',
            settings.lock_timeout_ms,
        )
        for partition in partitions:
            attached = await ddl.scalar(
                text(
                    "SELECT 1 FROM pg_inherits WHERE inhrelid = to_regclass(:i) "
                    "AND inhparent = to_regclass(:p)"
                ),
                {"i": f"{partition}_values_idx", "p": VALUES_INDEX},
            )
            if not attached:
                await _ddl(
                    ddl,
                    f"ALTER INDEX {VALUES_INDEX} ATTACH PARTITION {partition}_values_idx",
                    settings.lock_timeout_ms,
                )
        report.add("values index", "done", "built")
    except _LockedError:
        report.add("values index", "deferred", "lock not granted; run again")


async def _lexicon_index(
    ddl: AsyncConnection, state: StorageState, wanted: bool, report: ApplyReport
) -> None:
    present = state.lexicon_index or LEXICON_INDEX in state.invalid_indexes
    if wanted == state.lexicon_index and (wanted or not present):
        return
    if wanted:
        if present:  # an invalid leftover of an interrupted concurrent build
            await ddl.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {LEXICON_INDEX}")
        await ddl.exec_driver_sql(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {LEXICON_INDEX} ON mat_lexicon "
            "USING gin (domain_id, branch, word gin_trgm_ops)"
        )
        report.add("lexicon index", "done", "built")
    else:
        await ddl.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {LEXICON_INDEX}")
        report.add("lexicon index", "done", "dropped")


async def _hnsw(
    ddl: AsyncConnection,
    state: StorageState,
    wanted: Precision,
    dims: int,
    report: ApplyReport,
    *,
    switch_wait_s: float,
) -> None:
    if wanted == "half" and (state.pgvector is None or state.pgvector < HALFVEC_MIN_VERSION):
        report.add(
            "embedding index", "skipped", f"pgvector {state.pgvector} has no halfvec; kept full"
        )
        wanted = "full"
    other: Precision = "full" if wanted == "half" else "half"
    if state.hnsw.get(wanted) and not state.hnsw.get(other):
        return
    if not state.hnsw.get(wanted):
        name = hnsw_index_name(dims, wanted)
        valid = await ddl.scalar(
            text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:i)"),
            {"i": name},
        )
        if valid is False:
            await ddl.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        await ddl.exec_driver_sql(hnsw_index_sql(dims, wanted))
    valid = await ddl.scalar(
        text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:i)"),
        {"i": hnsw_index_name(dims, wanted)},
    )
    if not valid:
        report.add("embedding index", "failed", f"{wanted} index not valid after the build")
        return
    if state.hnsw.get(other):
        # Searches use the newest valid index (`embeddings.index_precision`, looked up again
        # after PRECISION_TTL_S): wait until every process uses the new one.
        await asyncio.sleep(switch_wait_s)
    await ddl.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {hnsw_index_name(dims, other)}")
    report.add("embedding index", "done", wanted)


async def _compression(
    ddl: AsyncConnection,
    state: StorageState,
    wanted: Compression,
    settings: StorageSettings,
    report: ApplyReport,
) -> None:
    changed = 0
    for table, columns in COMPRESSED.items():
        targets = await _partitions(ddl, table)
        if targets != [table]:
            targets = [table, *targets]
        for target in targets:
            for column in columns:
                current = state.compression.get(f"{target}.{column}")
                if current == wanted:
                    continue
                try:
                    await _ddl(
                        ddl,
                        f'ALTER TABLE {target} ALTER COLUMN "{column}" SET COMPRESSION {wanted}',
                        settings.lock_timeout_ms,
                    )
                    changed += 1
                except _LockedError:
                    report.add("compression", "deferred", "lock not granted; run again")
                    return
    report.add("compression", "done" if changed else "skipped", f"{wanted}, {changed} columns")


async def _fillfactor(
    ddl: AsyncConnection,
    state: StorageState,
    wanted: int,
    settings: StorageSettings,
    report: ApplyReport,
) -> None:
    if state.records_fillfactor == wanted:
        return
    try:
        for target in await _partitions(ddl, "mat_records"):
            await _ddl(
                ddl,
                f"ALTER TABLE {target} SET (fillfactor = {int(wanted)})",
                settings.lock_timeout_ms,
            )
        report.add("records fillfactor", "done", f"{wanted} (pages written from now on)")
    except _LockedError:
        report.add("records fillfactor", "deferred", "lock not granted; run again")


async def _attribute_indexes(
    engine: AsyncEngine, ddl: AsyncConnection, store: StoreSettings, report: ApplyReport
) -> None:
    """Build and drop per-Attribute indexes, concurrently, so every branch of every Domain
    has those the profile keeps (`indexes.pending_index_plan`)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from lorebase_store.indexes import pending_index_plan
    from lorebase_store.repositories.scope import scope_to_domain

    async with engine.connect() as connection:
        domains = [str(d) for d in (await connection.execute(text(_DOMAINS))).scalars()]
    statements: list[str] = []
    for domain_id in domains:
        async with engine.begin() as connection, AsyncSession(bind=connection) as session:
            await scope_to_domain(session, domain_id)
            statements.extend(
                await pending_index_plan(
                    session, domain_id, constraints_only=constraints_only(store)
                )
            )
    for statement in statements:
        await ddl.exec_driver_sql(statement)
    created = sum(1 for s in statements if s.startswith("CREATE"))
    dropped = sum(1 for s in statements if s.startswith("DROP"))
    report.add(
        "attribute indexes",
        "done" if statements else "skipped",
        f"{created} built, {dropped} dropped",
        len(statements),
    )


_DOMAINS = "SELECT id FROM domains ORDER BY id"


# Compaction ---------------------------------------------------------------------------------------


async def _vacuum(ddl: AsyncConnection, table: str) -> None:
    """Vacuum that always removes dead line pointers (it may skip index cleanup when few rows
    are dead, which would leave the emptied last pages in place) and truncates them."""
    await ddl.exec_driver_sql(f"VACUUM (ANALYZE, INDEX_CLEANUP ON, TRUNCATE ON) {table}")


_MOVE = (
    "WITH moved AS (DELETE FROM {table} WHERE ctid >= CAST(:lo AS tid) "
    "AND ctid < CAST(:hi AS tid) AND domain_id = :d AND branch = :br RETURNING *) "
    "INSERT INTO {table} SELECT * FROM moved RETURNING (ctid::text::point)[0]"
)
"""Moves the rows of one branch in the pages `[:lo, :hi)` to wherever the free space map
points; the pages each row landed on come back."""

_BRANCHES_IN = (
    "SELECT DISTINCT domain_id, branch FROM {table} "
    "WHERE ctid >= CAST(:lo AS tid) AND ctid < CAST(:hi AS tid)"
)

COMPACT_ROUNDS = 3
"""Passes over a table: each VACUUM gives back the emptied end and records the space freed in
the middle, which the next pass can fill."""


async def _move_window(
    engine: AsyncEngine,
    table: str,
    lo: int,
    hi: int,
    branches: Sequence[Any],
    settings: StorageSettings,
) -> int:
    """Move the rows of pages `[lo, hi)`, one branch at a time; raises `_NoRoomError` when a
    row would land at `lo` or later (the transaction is rolled back)."""
    moved = 0
    for domain_id, branch in branches:
        async with engine.begin() as connection:
            await connection.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
            await connection.exec_driver_sql(
                f"SET LOCAL lock_timeout = '{int(settings.lock_timeout_ms)}ms'"
            )
            # Commits of the branch take the same lock: none changes a row in flight.
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(:k)"),
                {"k": advisory_lock_key(str(domain_id), str(branch))},
            )
            params = {"lo": (lo, 0), "hi": (hi, 0), "d": domain_id, "br": branch}
            found = await connection.execute(text(_MOVE.format(table=table)), params)
            landed = found.scalars().all()
            if landed and max(landed) >= lo:
                raise _NoRoomError
            moved += len(landed)
    return moved


async def _compact_table(
    engine: AsyncEngine,
    ddl: AsyncConnection,
    table: str,
    settings: StorageSettings,
    report: ApplyReport,
    *,
    reindex: bool,
    max_batches: int | None,
) -> None:
    """Move the rows of the last pages into free space of earlier pages, window by window
    from the end, until no room is left below the window; then VACUUM truncates the end."""
    await _vacuum(ddl, table)
    before = await _pages(ddl, table)
    moved = batches = 0
    stalled = ""
    for _ in range(COMPACT_ROUNDS):
        pages = await _pages(ddl, table)
        per_page = await _rows_per_page(ddl, table)
        window = max(1, int(settings.batch_rows / max(per_page, 1.0)))
        hi, moved_now = pages, 0
        while hi > 0:
            if max_batches is not None and batches >= max_batches:
                stalled = "batch limit; run again"
                break
            lo = max(0, hi - window)
            found = await ddl.execute(
                text(_BRANCHES_IN.format(table=table)), {"lo": (lo, 0), "hi": (hi, 0)}
            )
            try:
                moved_now += await _move_window(engine, table, lo, hi, found.all(), settings)
            except _NoRoomError:
                break
            except DBAPIError as error:
                if sqlstate(error) in (LOCK_NOT_AVAILABLE, DEADLOCK):
                    stalled = "branch lock not granted; run again"
                    break
                raise
            batches += 1
            hi = lo
        await _vacuum(ddl, table)
        moved += moved_now
        if stalled or moved_now == 0 or await _pages(ddl, table) >= pages:
            break
    after = await _pages(ddl, table)
    if reindex:
        for target in await _partitions(ddl, table):
            await ddl.exec_driver_sql(f"REINDEX TABLE CONCURRENTLY {target}")
    detail = f"{before} -> {after} pages, {moved} rows moved" + (f" ({stalled})" if stalled else "")
    report.add(f"compact {table}", "deferred" if stalled else "done", detail, moved)


async def compact(
    engine: AsyncEngine,
    settings: StorageSettings,
    report: ApplyReport,
    *,
    tables: Sequence[str] = COMPACTED,
    reindex: bool = False,
    max_batches: int | None = None,
) -> None:
    """`--compact`: shrink the materialized tables (see the module documentation)."""
    ddl = await _autocommit(engine)
    try:
        for table in tables:
            for partition in await _partitions(ddl, table):
                if await _pages(ddl, partition) == 0:
                    continue
                await _compact_table(
                    engine,
                    ddl,
                    partition,
                    settings,
                    report,
                    reindex=reindex,
                    max_batches=max_batches,
                )
    finally:
        await ddl.close()


async def apply(
    engine: AsyncEngine,
    store: StoreSettings,
    *,
    compact_tables: bool = False,
    reindex: bool = False,
    max_batches: int | None = None,
    dry_run: bool = False,
    switch_wait_s: float = PRECISION_TTL_S + 1,
) -> ApplyReport:
    """Bring the database in line with the storage profile (see the module documentation)."""
    settings = store.storage
    choices = storage_choices(settings)
    report = ApplyReport(settings.profile, asdict(choices))
    dims = store.embeddings.dims
    ddl = await _autocommit(engine)
    try:
        state = await read_state(ddl, dims)
        if dry_run:
            for step, have, want in _differences(state, choices):
                report.add(step, "planned", f"{have} -> {want}")
            return report
        shared = choices.lineage_maps == "shared"
        triggers_ok = await _switch_triggers(ddl, state, shared, settings, report)
        await _values_index(ddl, state, choices.values_index, settings, report)
        await _lexicon_index(ddl, state, choices.lexicon_index, report)
        await _hnsw(
            ddl, state, choices.embedding_precision, dims, report, switch_wait_s=switch_wait_s
        )
        await _compression(ddl, state, choices.compression, settings, report)
        await _fillfactor(ddl, state, choices.records_fillfactor, settings, report)
        await _attribute_indexes(engine, ddl, store, report)
    finally:
        await ddl.close()
    if triggers_ok:
        await _rewrite_lineage(
            engine, settings, shared, report, max_batches=max_batches, leftovers=state.lineage_rows
        )
    else:
        report.add("lineage rows", "deferred", "triggers not switched yet")
    if compact_tables:
        await compact(engine, settings, report, reindex=reindex, max_batches=max_batches)
    if choices.prewarm:
        warmed = await prewarm(engine, settings)
        report.add(
            "prewarm",
            "done" if "error" not in warmed else "skipped",
            warmed.get("error") or f"{warmed['loaded_mb']} MB into shared buffers",
        )
    return report


def _differences(state: StorageState, choices: StorageChoices) -> list[tuple[str, Any, Any]]:
    shared = choices.lineage_maps == "shared"
    found: list[tuple[str, Any, Any]] = []
    for table, enabled in state.lineage_triggers.items():
        if enabled != shared:
            found.append((f"lineage trigger {table}", enabled, shared))
    for table, rows in state.lineage_rows.items():
        if rows["inline" if shared else "shared"]:
            found.append(
                (f"lineage rows {table}", f"{rows} in a 0.1% sample", choices.lineage_maps)
            )
    if state.values_index != choices.values_index:
        found.append(("values index", state.values_index, choices.values_index))
    if state.lexicon_index != choices.lexicon_index:
        found.append(("lexicon index", state.lexicon_index, choices.lexicon_index))
    have = "half" if state.hnsw.get("half") and not state.hnsw.get("full") else "full"
    if have != choices.embedding_precision:
        found.append(("embedding index", have, choices.embedding_precision))
    off = sorted(k for k, v in state.compression.items() if v != choices.compression)
    if off:
        found.append(("compression", f"{len(off)} columns", choices.compression))
    if state.records_fillfactor != choices.records_fillfactor:
        found.append(("records fillfactor", state.records_fillfactor, choices.records_fillfactor))
    return found


# Prewarm ------------------------------------------------------------------------------------------

HOT_RELATIONS: tuple[tuple[str, str], ...] = (
    # (relation or pattern, why). Order is priority: small and index pages first, then heaps.
    ("refs", "branch heads"),
    ("revisions", "Revisions"),
    ("mat_schema", "compiled schemas"),
    ("provenance", "provenance"),
    ("mat_lineage_maps", "shared lineage maps"),
    ("mat_lexicon", "word lexicon"),
    ("index:mat_lexicon", "lexicon indexes"),
    ("index:mat_lineage_maps", "lineage map key"),
    ("pkey:mat_records", "Record keys"),
    ("pkey:mat_links", "Link keys"),
    ("reverse:mat_links", "incoming Links"),
    ("search:mat_records", "full-text search"),
    ("attr:mat_records", "per-Attribute indexes"),
    ("hnsw:embeddings", "vector search"),
    ("heap:mat_records", "Records"),
    ("heap:mat_links", "Links"),
    ("pkey:mat_history", "history keys"),
    ("heap:mat_history", "history"),
    ("heap:embeddings", "vectors"),
)


async def _hot_relations(connection: AsyncConnection) -> list[tuple[str, int, str]]:
    """(relation, bytes, why) in prewarm order; partitions expanded, empty ones skipped."""
    out: list[tuple[str, int, str]] = []

    async def add(names: Sequence[str], why: str) -> None:
        for name in names:
            size = int(
                await connection.scalar(
                    text("SELECT pg_relation_size(to_regclass(:r))"), {"r": name}
                )
                or 0
            )
            if size > 0:
                out.append((name, size, why))

    async def indexes_of(table: str, where: str) -> list[str]:
        rows = await connection.execute(
            text(
                "SELECT ic.relname FROM pg_index x JOIN pg_class ic ON ic.oid = x.indexrelid "
                "JOIN pg_class t ON t.oid = x.indrelid "
                "WHERE (t.relname = :t OR t.oid IN (SELECT inhrelid FROM pg_inherits "
                "WHERE inhparent = to_regclass(:t))) AND ic.relkind = 'i' AND "
                + where
                + " ORDER BY ic.relname"
            ),
            {"t": table},
        )
        return [str(r) for r in rows.scalars()]

    for spec, why in HOT_RELATIONS:
        kind, _, table = spec.partition(":")
        if not table:
            await add(await _partitions(connection, kind), why)
        elif kind == "heap":
            await add(await _partitions(connection, table), why)
        elif kind == "index":
            await add(await indexes_of(table, "true"), why)
        elif kind == "pkey":
            await add(await indexes_of(table, "x.indisprimary"), why)
        elif kind == "reverse":
            await add(await indexes_of(table, "ic.relname LIKE '%to_key%'"), why)
        elif kind == "search":
            await add(await indexes_of(table, "ic.relname LIKE '%search%'"), why)
        elif kind == "attr":
            await add(await indexes_of(table, "ic.relname LIKE 'mat_records_attr_%'"), why)
        elif kind == "hnsw":
            await add(await indexes_of(table, "ic.relname LIKE 'embeddings_hnsw_%'"), why)
    return out


async def prewarm(
    engine: AsyncEngine, settings: StorageSettings, *, fraction: float | None = None
) -> dict[str, Any]:
    """Load the hot relations into shared buffers, most valuable first, within the budget."""
    connection = await _autocommit(engine)
    try:
        try:
            await connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
        except DBAPIError as error:
            return {"loaded": [], "error": f"pg_prewarm is not available: {error.orig}"}
        shared = int(
            await connection.scalar(
                text(
                    "SELECT setting::bigint * pg_size_bytes(coalesce(unit, '8kB')) "
                    "FROM pg_settings WHERE name = 'shared_buffers'"
                )
            )
            or 0
        )
        budget = int(shared * (fraction or settings.prewarm_fraction))
        loaded: list[dict[str, Any]] = []
        used = 0
        for relation, size, why in await _hot_relations(connection):
            if used >= budget:
                break
            blocks = min(size, budget - used) // 8192
            if blocks <= 0:
                break
            count = await connection.scalar(
                text("SELECT pg_prewarm(to_regclass(:r), 'buffer', 'main', 0, :last)"),
                {"r": relation, "last": blocks - 1},
            )
            used += int(count or 0) * 8192
            loaded.append(
                {
                    "relation": relation,
                    "why": why,
                    "mb": round(int(count or 0) * 8192 / 2**20, 1),
                    "complete": blocks * 8192 >= size,
                }
            )
        preload = str(await connection.scalar(text("SHOW shared_preload_libraries")) or "")
        return {
            "shared_buffers_mb": round(shared / 2**20),
            "budget_mb": round(budget / 2**20),
            "loaded_mb": round(used / 2**20, 1),
            "autoprewarm": "pg_prewarm" in preload,
            "loaded": loaded,
        }
    finally:
        await connection.close()


# Status -------------------------------------------------------------------------------------------


async def status(engine: AsyncEngine, store: StoreSettings) -> dict[str, Any]:
    """Choices, what the database has, open steps and the size of the hot relations."""
    choices = storage_choices(store.storage)
    async with engine.connect() as connection:
        await connection.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', false)"))
        state = await read_state(connection, store.embeddings.dims)
        hot = await _hot_relations(connection)
        sizes = [{"relation": r, "mb": round(b / 2**20, 1), "why": w} for r, b, w in hot]
        tasks = {
            str(name): (json.loads(s) if isinstance(s, str) else s)
            for name, s in await connection.execute(text("SELECT name, state FROM storage_tasks"))
        }
    return {
        "profile": store.storage.profile,
        "choices": asdict(choices),
        "state": asdict(state),
        "pending": [{"step": s, "have": h, "want": w} for s, h, w in _differences(state, choices)],
        "tasks": tasks,
        "hot_relations": sizes,
        "hot_mb": round(sum(b for _, b, _ in hot) / 2**20, 1),
    }


# Command line -------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lorebase_store.storage",
        description="Storage profile of the database (store.storage).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Choices, what the database has, hot relation sizes.")
    run = commands.add_parser("apply", help="Bring the database in line with the profile.")
    run.add_argument("--dry-run", action="store_true", help="Only list what would change.")
    run.add_argument(
        "--compact", action="store_true", help="Also move rows into free space and truncate."
    )
    run.add_argument(
        "--reindex", action="store_true", help="With --compact: rebuild indexes concurrently."
    )
    run.add_argument(
        "--max-batches", type=int, default=None, help="Stop after this many batches (resumable)."
    )
    warm = commands.add_parser("prewarm", help="Load the hot relations into shared buffers.")
    warm.add_argument("--fraction", type=float, default=None, help="Share of shared_buffers.")
    return parser


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    from lorebase_platform.settings import load_settings
    from lorebase_store.db import create_engine
    from lorebase_store.settings import database_settings, store_settings

    loaded = load_settings()
    database = database_settings(loaded)
    store = store_settings(loaded)
    url = None if database.maintenance_dsn is None else database.maintenance_dsn.get_secret_value()
    engine = create_engine(database, url=url, pooled=False, statement_timeout_ms=0)
    try:
        if args.command == "status":
            return await status(engine, store)
        if args.command == "prewarm":
            return await prewarm(engine, store.storage, fraction=args.fraction)
        report = await apply(
            engine,
            store,
            compact_tables=args.compact,
            reindex=args.reindex,
            max_batches=args.max_batches,
            dry_run=args.dry_run,
        )
        return report.as_dict()
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parser().parse_args(argv)
    result = asyncio.run(_run(args))
    sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")
    if args.command == "apply" and not result.get("complete", True):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
