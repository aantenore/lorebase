"""Settings sections owned by the store, registered with `lorebase_platform`.

- `database` (design 16): connection string, pool and timeouts, optional read replicas. The
  environment alias `LOREBASE_DB_DSN` sets `database.dsn` and `LOREBASE_DB_READ_DSN` sets
  `database.read_dsn`; both are handled as secrets.
- `store`: physical layout and vector storage (read by the initial migration), bulk writes,
  tree node caching, commits, materialization, per-Attribute indexes, reads, neighborhood
  traversal and the profilers of schema inference (read at runtime).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator

from lorebase_platform.secrets import SecretValue
from lorebase_platform.settings import SectionModel, Settings, register_section

DATABASE_SECTION = "database"
STORE_SECTION = "store"
ENV_ALIASES = {"LOREBASE_DB_DSN": "dsn", "LOREBASE_DB_READ_DSN": "read_dsn"}
DEFAULT_DSN = "postgresql+asyncpg://lorebase@localhost:5432/lorebase"

PGVECTOR_HNSW_MAX_DIMS = 2000
"""Largest `vector` dimension pgvector can index with HNSW."""


class DatabaseSettings(SectionModel):
    """Connection and pool of the Postgres database."""

    dsn: SecretValue = Field(
        default=SecretStr(DEFAULT_DSN),
        description="SQLAlchemy URL; postgresql:// and postgres:// are switched to the asyncpg "
        "driver. Literal or secret reference, e.g. secret://env/LOREBASE_DB_DSN (alias "
        "LOREBASE_DB_DSN). Without a password in the URL, asyncpg reads PGPASSWORD or ~/.pgpass.",
    )
    pool_size: int = Field(default=10, ge=1, description="Connections kept open per process.")
    max_overflow: int = Field(
        default=10, ge=0, description="Extra connections opened under load above pool_size."
    )
    pool_timeout_s: float = Field(
        default=30, gt=0, description="Seconds to wait for a free pooled connection."
    )
    pool_recycle_s: int = Field(
        default=1800, ge=-1, description="Connections older than this are replaced; -1 never."
    )
    statement_timeout_ms: int = Field(
        default=30000, ge=0, description="Postgres statement_timeout per connection; 0 disables."
    )
    migration_statement_timeout_ms: int = Field(
        default=0,
        ge=0,
        description="statement_timeout while migrations run; 0 disables (index builds are long).",
    )
    application_name: str = Field(
        default="lorebase", min_length=1, description="Reported in pg_stat_activity."
    )
    application_role: str | None = Field(
        default=None,
        min_length=1,
        description="Database role the services connect as (subject to row-level security). "
        "Read by migrations only: revision 0004 grants it EXECUTE on the security definer "
        "read functions, which are revoked from PUBLIC. Empty grants nothing (the owner role "
        "can still call them).",
    )
    read_dsn: tuple[SecretValue, ...] = Field(
        default=(),
        description="Read replicas (design 15.3): one URL, a comma-separated string or a list "
        "of URLs, each a literal or secret reference. Reads are spread round-robin; empty "
        "keeps every read on the primary. Alias LOREBASE_DB_READ_DSN. Each replica gets its "
        "own pool sized like the primary's.",
    )
    read_max_lag_ms: int = Field(
        default=5000,
        ge=0,
        description="A replica whose replay lag exceeds this serves no reads; 0 disables the "
        "lag check (the head check of a pinned read still applies).",
    )
    read_fallback_to_primary: bool = Field(
        default=True,
        description="Reads that no replica can serve (stale, lagging, unreachable) go to the "
        "primary; false answers 503 instead, protecting the primary from read load.",
    )
    read_probe_timeout_ms: int = Field(
        default=2000,
        ge=1,
        description="Time budget to reach a replica and run its freshness probe before the "
        "next replica or the fallback is tried.",
    )

    @field_validator("read_dsn", mode="before")
    @classmethod
    def _split_read_dsn(cls, value: Any) -> Any:
        if value is None:
            return ()
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    prepared_statement_cache_size: int = Field(
        default=500,
        ge=0,
        description="Prepared statements kept per connection by the asyncpg driver, keyed by "
        "SQL text; 0 disables the cache.",
    )


class LayoutSettings(SectionModel):
    """Physical layout fixed when the schema is installed (first migration)."""

    hash_partitions: int = Field(
        default=8,
        ge=1,
        le=1024,
        description="Hash partitions on domain_id for objects, tree_nodes, mat_records and "
        "mat_links. Read only by the initial migration; changing it later has no effect.",
    )


class EmbeddingStoreSettings(SectionModel):
    """Vector storage. Must agree with `inference.embeddings`."""

    dims: int = Field(
        default=384,
        ge=1,
        le=PGVECTOR_HNSW_MAX_DIMS,
        description="Dimension of the HNSW index created on `embeddings`; equals "
        "inference.embeddings.dims.",
    )


class WriteSettings(SectionModel):
    """Bulk writes of objects, tree nodes, changesets and materialized rows."""

    insert_batch_rows: int = Field(
        default=1000, ge=1, description="Rows per multi-row INSERT statement."
    )
    copy_threshold_rows: int = Field(
        default=5000,
        ge=0,
        description="Batches with at least this many rows go through COPY into a staging table "
        "and one set-based INSERT; 0 disables COPY.",
    )


class NodeSettings(SectionModel):
    """Tree node reads (design 6.14: nodes are immutable and cached by hash)."""

    cache_bytes: int = Field(
        default=64 * 1024 * 1024,
        ge=0,
        description="In-process LRU cache of tree node bodies, in bytes; 0 disables it.",
    )
    prefetch_batch: int = Field(
        default=1000, ge=1, description="Node hashes fetched per query when prefetching."
    )


class CommitSettings(SectionModel):
    """Commit pipeline (design 6.6)."""

    lock_timeout_ms: int = Field(
        default=10000,
        ge=0,
        description="Wait for the branch advisory lock before failing; 0 waits without limit.",
    )
    prefetch_batch: int = Field(
        default=1000,
        ge=1,
        description="Keys per query when validation prefetches materialized Records and Links.",
    )


class MaterializeSettings(SectionModel):
    """Materialized heads (design 6.11, 8.4)."""

    text_search_config: str = Field(
        default="simple",
        min_length=1,
        description="Postgres text search configuration of the Record search vectors.",
    )
    read_batch_rows: int = Field(
        default=5000,
        ge=1,
        description="Rows or object bodies read per query by rebuild and verification.",
    )
    verify_max_divergences: int = Field(
        default=1000, ge=1, description="Divergences listed by a verification report."
    )


class GcSettings(SectionModel):
    """Garbage collection of unreachable Revisions, tree nodes and objects (design 6.13)."""

    grace_seconds: int = Field(
        default=3600,
        ge=0,
        description="Revisions younger than this are kept with everything they reach, even when "
        "no ref reaches them. Protects commits that land while a collection runs.",
    )
    mark_batch: int = Field(
        default=1000, ge=1, description="Tree node hashes read per query while marking."
    )
    sweep_batch_rows: int = Field(
        default=5000, ge=1, description="Rows deleted per transaction while sweeping."
    )
    guard_branch_locks: bool = Field(
        default=True,
        description="Take every branch lock of the Domain (without waiting) around each sweep "
        "batch, so no commit that reuses a stored object is in flight while it is deleted. "
        "Off: only the grace period protects commits.",
    )
    max_guarded_branches: int = Field(
        default=500,
        ge=1,
        description="Largest number of refs whose locks one sweep batch takes; a Domain with "
        "more refs needs a larger value (mind max_locks_per_transaction) or guard_branch_locks "
        "off.",
    )
    guard_wait_seconds: float = Field(
        default=60,
        gt=0,
        description="How long a sweep batch waits for commits holding a branch lock before the "
        "collection stops; progress already made is kept.",
    )
    guard_retry_ms: int = Field(
        default=200, ge=1, description="Pause between attempts to take the branch locks."
    )


class JobSettings(SectionModel):
    """Job queue workers (design 7.4): claiming, retries and stale locks."""

    max_attempts: int = Field(
        default=3, ge=1, description="Runs of a job before it is marked failed."
    )
    retry_delay_seconds: float = Field(
        default=60, ge=0, description="Delay before a failed job is run again."
    )
    stale_after_seconds: float = Field(
        default=3600,
        gt=0,
        description="A running job locked for longer than this is assumed lost and requeued.",
    )


class BundleSettings(SectionModel):
    """Domain export and import bundles (see docs/operations.md)."""

    batch_rows: int = Field(
        default=5000, ge=1, description="Rows read, written or verified per batch."
    )
    compression: Literal["gzip", "none"] = Field(
        default="gzip", description="Compression of the bundle's .jsonl files."
    )
    compression_level: int = Field(
        default=6, ge=1, le=9, description="gzip level (1 fastest, 9 smallest)."
    )
    verify_after_import: bool = Field(
        default=True,
        description="Verify every rebuilt branch against its tree after an import.",
    )


class CsvProfilingSettings(SectionModel):
    """Reading CSV and TSV files for schema inference."""

    sample_rows: int = Field(
        default=10000, ge=1, description="Rows kept per file; every row is still counted."
    )
    sampling: Literal["head", "reservoir"] = Field(
        default="reservoir",
        description="head keeps the first rows; reservoir keeps a uniform, seeded sample.",
    )
    seed: int = Field(default=0, description="Seed of reservoir sampling (same file, same rows).")
    encodings: tuple[str, ...] = Field(
        default=("utf-8-sig", "cp1252"),
        min_length=1,
        description="Encodings tried in order; the first that decodes the file wins.",
    )
    sniff_bytes: int = Field(
        default=65536, ge=1024, description="Leading bytes used to detect the delimiter."
    )
    delimiters: str = Field(
        default=",;\t|", min_length=1, description="Delimiters the dialect sniffer may pick."
    )
    max_columns: int = Field(default=500, ge=1, description="Wider files are refused.")


class PostgresProfilingSettings(SectionModel):
    """Reading Postgres tables for schema inference: catalog reflection plus sampled SELECTs."""

    sample_rows: int = Field(default=10000, ge=1, description="Rows read per table (LIMIT).")
    max_tables: int = Field(default=200, ge=1, description="Tables profiled per request.")
    statement_timeout_ms: int = Field(
        default=15000, ge=1, description="statement_timeout of each sampling query."
    )
    exact_row_count: bool = Field(
        default=False,
        description="count(*) each table; false uses the planner estimate (pg_class.reltuples).",
    )


class ProfilingSettings(SectionModel):
    """Profilers that feed schema inference (design 9.1)."""

    csv: CsvProfilingSettings = Field(default_factory=CsvProfilingSettings)
    postgres: PostgresProfilingSettings = Field(default_factory=PostgresProfilingSettings)


class IndexSettings(SectionModel):
    """Per-Attribute partial indexes on `mat_records` (`lorebase_store.indexes`)."""

    enabled: bool = Field(
        default=True,
        description="Create and drop per-Attribute indexes when schemas change. Only a role "
        "that owns mat_records does so; other roles skip it with a warning. False leaves index "
        "management to an administrator.",
    )
    inline_max_rows: int = Field(
        default=50000,
        ge=0,
        description="Kinds with at most this many Records on the branch get their indexes "
        "inside the commit transaction; larger ones are queued as jobs and built concurrently.",
    )
    job_batch: int = Field(
        default=10, ge=1, description="Queued index jobs claimed per run of the job runner."
    )


class ReadSettings(SectionModel):
    """Read primitives on materialized heads (`lorebase_store.reads`)."""

    schema_cache_entries: int = Field(
        default=256,
        ge=0,
        description="Compiled schemas cached per process, keyed by Domain and head Revision "
        "id; 0 disables the cache.",
    )
    max_page_rows: int = Field(
        default=500, ge=1, description="Largest page of Records, Links or query results."
    )
    max_search_rows: int = Field(
        default=100, ge=1, description="Largest number of full-text search results."
    )
    search_candidates: int = Field(
        default=1000,
        ge=1,
        description="Matching Records ranked per search; ranking reads each candidate's search "
        "vector, so this bounds the cost of very common terms.",
    )
    max_hydrate_keys: int = Field(
        default=1000, ge=1, description="Records fetched by one batched hydration."
    )
    use_definer_functions: bool = Field(
        default=True,
        description="Run search, incoming Links, neighborhoods and attribute queries through "
        "the security definer functions of revision 0004, whose index plans survive row-level "
        "security; false runs the same statements as plain SQL.",
    )


class TraverseSettings(SectionModel):
    """Neighborhood traversal over `mat_links` (`lorebase_store.traverse`)."""

    max_depth: int = Field(default=3, ge=1, le=10, description="Deepest neighborhood, in hops.")
    max_seeds: int = Field(default=50, ge=1, description="Seed Records of one neighborhood.")
    max_fanout: int = Field(
        default=50, ge=1, description="Links followed from one Record in one hop."
    )
    max_links: int = Field(
        default=2000,
        ge=1,
        description="Links walked by one neighborhood; the walk stops, breadth first, there.",
    )
    max_nodes: int = Field(
        default=200, ge=1, description="Records returned by one neighborhood, nearest first."
    )


class StoreSettings(SectionModel):
    """Schema shape, bulk writes, node cache, commits, materialization, indexes, reads and
    profiling."""

    layout: LayoutSettings = Field(default_factory=LayoutSettings)
    embeddings: EmbeddingStoreSettings = Field(default_factory=EmbeddingStoreSettings)
    writes: WriteSettings = Field(default_factory=WriteSettings)
    nodes: NodeSettings = Field(default_factory=NodeSettings)
    commit: CommitSettings = Field(default_factory=CommitSettings)
    materialize: MaterializeSettings = Field(default_factory=MaterializeSettings)
    gc: GcSettings = Field(default_factory=GcSettings)
    jobs: JobSettings = Field(default_factory=JobSettings)
    bundle: BundleSettings = Field(default_factory=BundleSettings)
    profiling: ProfilingSettings = Field(default_factory=ProfilingSettings)
    indexes: IndexSettings = Field(default_factory=IndexSettings)
    reads: ReadSettings = Field(default_factory=ReadSettings)
    traverse: TraverseSettings = Field(default_factory=TraverseSettings)


register_section(DATABASE_SECTION, DatabaseSettings, env_aliases=ENV_ALIASES)
register_section(STORE_SECTION, StoreSettings)


def database_settings(settings: Settings) -> DatabaseSettings:
    """The validated `database` section of loaded settings."""
    return settings.section(DATABASE_SECTION, DatabaseSettings)


def store_settings(settings: Settings) -> StoreSettings:
    """The validated `store` section of loaded settings."""
    return settings.section(STORE_SECTION, StoreSettings)
