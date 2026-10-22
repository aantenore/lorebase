"""Settings sections owned by the store, registered with `lorebase_platform`.

- `database` (design 16): connection string, pool and timeouts, optional read replicas. The
  environment alias `LOREBASE_DB_DSN` sets `database.dsn`, `LOREBASE_DB_READ_DSN` sets
  `database.read_dsn` and `LOREBASE_DB_MAINTENANCE_DSN` sets `database.maintenance_dsn`; all
  are handled as secrets.
- `store`: physical layout and vector storage (read by the initial migration), bulk writes,
  tree node caching, commits, materialization, per-Attribute indexes, reads, neighborhood
  traversal and the profilers of schema inference (read at runtime).
- `schema`: static validation of schemas, shared by Keeper and the store so that every path
  that validates a schema (schema change sets, the commit pipeline, merges, schema inference)
  applies the same rules: whether CEL expressions are accepted and their budget.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator

from lorebase_core.expr import ExprLimits
from lorebase_core.infer.suggestions import SuggestionOptions
from lorebase_core.validate import SchemaRules
from lorebase_platform.secrets import SecretValue
from lorebase_platform.settings import SectionModel, Settings, register_section

DATABASE_SECTION = "database"
STORE_SECTION = "store"
SCHEMA_SECTION = "schema"
ENV_ALIASES = {
    "LOREBASE_DB_DSN": "dsn",
    "LOREBASE_DB_READ_DSN": "read_dsn",
    "LOREBASE_DB_MAINTENANCE_DSN": "maintenance_dsn",
}
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
    maintenance_dsn: SecretValue | None = Field(
        default=None,
        description="Optional URL of a role that owns mat_records (or may create indexes on "
        "it), used only for maintenance DDL: the job worker builds and drops the per-Attribute "
        "indexes with it. Literal or secret reference (alias LOREBASE_DB_MAINTENANCE_DSN). "
        "Unset: only a commit role that owns mat_records manages the indexes.",
    )
    application_role: str | None = Field(
        default=None,
        min_length=1,
        description="Database role the services connect as (subject to row-level security). "
        "Read by migrations only: revisions 0005 and 0008 grant it EXECUTE on the security "
        "definer read functions, which are revoked from PUBLIC, and 0008 refuses to run while "
        "it may create objects in the schema of the tables or of pg_trgm; 0015 revokes UPDATE, "
        "DELETE and TRUNCATE on audit_log from it. Empty grants nothing (the owner role can "
        "still call them).",
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
    derivations_enabled: bool = Field(
        default=True,
        description="Recompute derived Attributes (expression and aggregate Derivations) and "
        "the Links of materialized path Derivations in the materialized head on every commit, "
        "merge and rebuild.",
    )
    derivation_max_incremental_records: int = Field(
        default=10000,
        ge=1,
        description="Records one Derivation recomputes incrementally in one commit (Records "
        "written plus Records whose Links or linked Records changed); above this the "
        "Derivation is recomputed for every Record of its Kind, in batches of read_batch_rows.",
    )
    branch_mode: Literal["copy", "overlay"] = Field(
        default="copy",
        description="How a new branch gets its materialized head when the request names no "
        "mode: copy (full rows, AC-16) or overlay (only the rows it changes, reading its base "
        "as of the fork point; store schema notes).",
    )
    overlay_branch_prefixes: tuple[str, ...] = Field(
        default=("agent/", "extract/", "source/"),
        description="Branches whose name starts with one of these prefixes default to "
        "overlay mode whatever branch_mode says.",
    )
    overlay_max_rows: int = Field(
        default=50000,
        ge=1,
        description="Rows (Records, Links and tombstones) an overlay may hold; a commit that "
        "leaves it with more promotes it to a full copy.",
    )
    overlay_max_base_lag_revisions: int = Field(
        default=1000,
        ge=1,
        description="Generations the base branch may move past an overlay's fork point; "
        "beyond, the overlay is promoted to a full copy (by its next commit, or by a "
        "store.materialize job the base commit queues).",
    )
    overlay_max_per_branch: int = Field(
        default=32,
        ge=0,
        description="Open overlays on one base branch (each base commit copies the rows it "
        "changes into each of them); further branches are created as copies.",
    )
    async_copy_min_rows: int = Field(
        default=10000,
        ge=0,
        description="A copy branch whose source holds more rows (Records and Links) is "
        "created as an overlay at once and turned into a full copy by a store.materialize "
        "job, in batches; smaller copies are made in the creating transaction.",
    )
    async_delete_min_rows: int = Field(
        default=10000,
        ge=0,
        description="Deleting a branch that holds more rows (Records and Links) hides it at "
        "once and leaves its rows to a store.branch_delete job, in batches; smaller branches "
        "are deleted in the deleting transaction.",
    )
    job_batch_rows: int = Field(
        default=5000,
        ge=1,
        description="Rows a store.materialize or store.branch_delete job copies or deletes "
        "per transaction; each batch must finish within database.statement_timeout_ms.",
    )
    job_concurrency: int = Field(
        default=2,
        ge=1,
        description="store.materialize and store.branch_delete jobs running at once across "
        "all workers; a job that finds every slot taken runs again after job_retry_seconds.",
    )
    job_retry_seconds: float = Field(
        default=10,
        ge=0,
        description="Delay before a branch job that found no free slot, a busy branch or a "
        "deletion waiting for its overlays runs again.",
    )
    refresh_branches_on_tag: bool = Field(
        default=True,
        description="When a commit or merge on a Domain's default branch adds a sensitivity "
        "tag to an existing Attribute, queue a store.sensitivity_refresh job for every other "
        "branch: it rewrites, in batches of job_batch_rows, the titles and search vectors the "
        "branch materialized before the tag (verification reports them as sensitivity_stale). "
        "The branch's schema is left as it is: POST .../branches/{b}/rebase-tags copies the "
        "tags into it. Off: stale rows stay until rewritten that way, by a rebuild or by the "
        "next schema change of their Kind on the branch.",
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


class OutboxSettings(SectionModel):
    """Retention of outbox events (design 10.5), shared by every consumer of the outbox."""

    retention_s: float = Field(
        default=7 * 24 * 3600.0,
        gt=0,
        description="Events every consumer has handed off are deleted once older than this. "
        "The change feed cannot resume from deleted events.",
    )
    max_age_s: float = Field(
        default=30 * 24 * 3600.0,
        gt=0,
        description="Events older than this are deleted even when a consumer has not handed "
        "them off yet (a consumer that stopped running); counted in lorebase.outbox.pruned "
        "as expired.",
    )
    prune_batch: int = Field(
        default=10000,
        ge=1,
        description="Events deleted per reason by one batch (its own short transaction). A "
        "pruning pass, run by each webhook delivery pass, repeats batches until nothing is "
        "left to prune or outbox.prune_budget_s is spent.",
    )
    prune_budget_s: float = Field(
        default=5.0,
        gt=0,
        description="Time one pruning pass may spend deleting batches of outbox.prune_batch "
        "events; no new batch starts once it is spent (a batch always runs), and the rest "
        "waits for the next pass. Events still prunable are reported by "
        "lorebase.outbox.prune.backlog.",
    )

    @model_validator(mode="after")
    def _ordered(self) -> OutboxSettings:
        if self.max_age_s < self.retention_s:
            raise ValueError("outbox.max_age_s cannot be shorter than outbox.retention_s")
        return self


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
    max_member_bytes: int = Field(
        default=17_179_869_184,
        ge=1,
        description="Largest file of a bundle being imported, counted as read (after gzip).",
    )
    max_total_bytes: int = Field(
        default=68_719_476_736,
        ge=1,
        description="Largest sum of the file sizes of a bundle being imported.",
    )
    max_entries: int = Field(
        default=100_000,
        ge=1,
        description="Most files and directories a bundle being imported may hold.",
    )
    max_line_bytes: int = Field(
        default=67_108_864,
        ge=1,
        description="Longest .jsonl record, and largest .json file, read into memory on import.",
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


class RecordProfilingSettings(SectionModel):
    """Profiling rows read as records (JSON, JSON Lines, Parquet and API Sources)."""

    sample_rows: int = Field(
        default=10000, ge=1, description="Rows kept per table; every row is still counted."
    )
    sampling: Literal["head", "reservoir"] = Field(
        default="reservoir",
        description="head keeps the first rows; reservoir keeps a uniform, seeded sample.",
    )
    seed: int = Field(default=0, description="Seed of reservoir sampling (same rows, same sample).")
    max_columns: int = Field(
        default=500, ge=1, description="Tables with more distinct columns are refused."
    )
    batch_rows: int = Field(
        default=10000, ge=1, description="Rows decoded per batch of an uploaded Parquet file."
    )


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


class SqlProfilingSettings(SectionModel):
    """Reading MySQL, MariaDB and SQL Server tables for schema inference: catalog reflection
    plus a bounded SELECT per table, profiled as records. The drivers are optional extras
    (`lorebase-keeper[mysql]`, `lorebase-keeper[mssql]`)."""

    sample_rows: int = Field(default=10000, ge=1, description="Rows read per table (LIMIT/TOP).")
    max_tables: int = Field(default=50, ge=1, description="Tables profiled per request.")
    total_timeout_s: float = Field(
        default=120.0,
        gt=0,
        description="Longest time a request may spend reading tables, checked before each "
        "table; the request is refused when it is exceeded.",
    )
    max_value_chars: int = Field(
        default=1000,
        ge=1,
        description="Longest text kept per value: longer text and unbounded text columns are "
        "cut when read; binary columns without a declared length up to this size, and columns "
        "of unknown type, are not read.",
    )
    statement_timeout_ms: int = Field(
        default=15000,
        ge=1,
        description="Longest execution of each query: max_execution_time (MySQL), "
        "max_statement_time (MariaDB, whole seconds), the driver timeout and LOCK_TIMEOUT "
        "(SQL Server).",
    )
    connect_timeout_s: float = Field(default=10.0, gt=0, description="Connection timeout.")
    count_rows: bool = Field(
        default=True,
        description="count(*) each table so that inference knows the sample is partial; false "
        "takes the sampled rows as the whole table.",
    )


class ProfilingSettings(SectionModel):
    """Profilers that feed schema inference (design 9.1)."""

    csv: CsvProfilingSettings = Field(default_factory=CsvProfilingSettings)
    records: RecordProfilingSettings = Field(default_factory=RecordProfilingSettings)
    postgres: PostgresProfilingSettings = Field(default_factory=PostgresProfilingSettings)
    sql: SqlProfilingSettings = Field(default_factory=SqlProfilingSettings)


class IndexSettings(SectionModel):
    """Per-Attribute partial indexes on `mat_records` (`lorebase_store.indexes`)."""

    enabled: bool = Field(
        default=True,
        description="Create and drop per-Attribute indexes when schemas change. A role that "
        "owns mat_records does so in the commit; other roles queue the changes for the job "
        "worker when database.maintenance_dsn is set, and skip them with a warning otherwise. "
        "False leaves index management to an administrator.",
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


class DefinerCeilingSettings(SectionModel):
    """Largest count arguments the security definer read functions accept.

    Read when revision 0008 runs and written into the functions, which refuse NULL and values
    outside 1..ceiling; changing them later has no effect. Each is at least the matching cap of
    `store.reads` or `store.traverse` (checked when the settings load).
    """

    limit: int = Field(
        default=10000,
        ge=1,
        description="Rows returned by lb_search, lb_links_in and lb_query_by_attribute; at "
        "least reads.max_page_rows and reads.max_search_rows.",
    )
    candidates: int = Field(
        default=100000,
        ge=1,
        description="Candidates ranked by lb_search; at least reads.search_candidates.",
    )
    depth: int = Field(
        default=10, ge=1, description="Hops of lb_neighborhood; at least traverse.max_depth."
    )
    fanout: int = Field(
        default=10000,
        ge=1,
        description="Links per Record and hop of lb_neighborhood; at least traverse.max_fanout.",
    )
    max_steps: int = Field(
        default=1000000,
        ge=1,
        description="Steps (Links walked plus seeds) of lb_neighborhood; at least "
        "traverse.max_links plus traverse.max_seeds.",
    )
    max_nodes: int = Field(
        default=100000,
        ge=1,
        description="Records returned by lb_neighborhood; at least traverse.max_nodes.",
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
        "the security definer functions of revision 0005, whose index plans survive row-level "
        "security; false runs the same statements as plain SQL.",
    )
    definer_ceilings: DefinerCeilingSettings = Field(default_factory=DefinerCeilingSettings)


class TraverseSettings(SectionModel):
    """Neighborhood traversal over `mat_links` (`lorebase_store.traverse`) and the fan-out of
    path Derivations (`lorebase_core.model.paths`)."""

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
    path_fanout: int = Field(
        default=100,
        ge=1,
        description="Links a path Derivation follows from one intermediate Record (smallest "
        "far keys first), at read time and when materialized; a new value rewrites the "
        "materialized paths and the derived values reading a path at the next commit of a "
        "branch, or when a job worker starts.",
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
    outbox: OutboxSettings = Field(default_factory=OutboxSettings)
    bundle: BundleSettings = Field(default_factory=BundleSettings)
    profiling: ProfilingSettings = Field(default_factory=ProfilingSettings)
    indexes: IndexSettings = Field(default_factory=IndexSettings)
    reads: ReadSettings = Field(default_factory=ReadSettings)
    traverse: TraverseSettings = Field(default_factory=TraverseSettings)

    @model_validator(mode="after")
    def _caps_within_definer_ceilings(self) -> StoreSettings:
        """The read primitives never pass the definer functions a count above its ceiling."""
        if not self.reads.use_definer_functions:
            return self
        ceilings, reads, walk = self.reads.definer_ceilings, self.reads, self.traverse
        for name, cap, ceiling in (
            ("reads.max_page_rows", reads.max_page_rows, ceilings.limit),
            ("reads.max_search_rows", reads.max_search_rows, ceilings.limit),
            ("reads.search_candidates", reads.search_candidates, ceilings.candidates),
            ("traverse.max_depth", walk.max_depth, ceilings.depth),
            ("traverse.max_fanout", walk.max_fanout, ceilings.fanout),
            ("traverse.max_links + max_seeds", walk.max_links + walk.max_seeds, ceilings.max_steps),
            ("traverse.max_nodes", walk.max_nodes, ceilings.max_nodes),
        ):
            if cap > ceiling:
                raise ValueError(
                    f"{name} ({cap}) exceeds its ceiling in reads.definer_ceilings ({ceiling})"
                )
        return self


class SchemaSettings(SectionModel):
    """Static validation of schemas (design 5.7, 5.8, AC-06), shared by Keeper and the store."""

    expressions_enabled: bool = Field(
        default=True,
        description="Accept Operations and Derivations written in CEL; every expression is "
        "parsed and type checked against the schema. False refuses them "
        "(schema.expressions_disabled) wherever a schema is validated.",
    )
    expression_limits: ExprLimits = Field(
        default_factory=ExprLimits,
        description="Budget of one schema expression: compiled at validation, evaluated by "
        "the materializer for Derivations (source length, nesting depth, evaluation steps "
        "and time, sizes of built strings and lists, Record lookups, decimal precision).",
    )
    suggestions: SuggestionOptions = Field(
        default_factory=SuggestionOptions,
        description="Schema suggestions aggregated from document extraction: support needed "
        "to list one, how many are listed, examples and samples kept, name matching against "
        "the schema (docs/extraction.md#schema-suggestions).",
    )

    def rules(self) -> SchemaRules:
        """Options of `validate_schema` for these settings."""
        return SchemaRules(
            allow_expressions=self.expressions_enabled,
            expression_limits=self.expression_limits,
        )


register_section(DATABASE_SECTION, DatabaseSettings, env_aliases=ENV_ALIASES)
register_section(STORE_SECTION, StoreSettings)
register_section(SCHEMA_SECTION, SchemaSettings)


def database_settings(settings: Settings) -> DatabaseSettings:
    """The validated `database` section of loaded settings."""
    return settings.section(DATABASE_SECTION, DatabaseSettings)


def store_settings(settings: Settings) -> StoreSettings:
    """The validated `store` section of loaded settings."""
    return settings.section(STORE_SECTION, StoreSettings)


def schema_settings(settings: Settings) -> SchemaSettings:
    """The validated `schema` section of loaded settings."""
    return settings.section(SCHEMA_SECTION, SchemaSettings)
