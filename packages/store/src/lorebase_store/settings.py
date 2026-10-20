"""Settings sections owned by the store, registered with `lorebase_platform`.

- `database` (design 16): connection string, pool and timeouts, optional read replicas. The
  environment alias `LOREBASE_DB_DSN` sets `database.dsn` and `LOREBASE_DB_READ_DSN` sets
  `database.read_dsn`; both are handled as secrets.
- `store`: physical layout and vector storage (read by the initial migration), bulk writes,
  tree node caching, commits and materialization (read at runtime).
"""

from __future__ import annotations

from typing import Any

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


class StoreSettings(SectionModel):
    """Schema shape, bulk writes, node cache, commits and materialization."""

    layout: LayoutSettings = Field(default_factory=LayoutSettings)
    embeddings: EmbeddingStoreSettings = Field(default_factory=EmbeddingStoreSettings)
    writes: WriteSettings = Field(default_factory=WriteSettings)
    nodes: NodeSettings = Field(default_factory=NodeSettings)
    commit: CommitSettings = Field(default_factory=CommitSettings)
    materialize: MaterializeSettings = Field(default_factory=MaterializeSettings)


register_section(DATABASE_SECTION, DatabaseSettings, env_aliases=ENV_ALIASES)
register_section(STORE_SECTION, StoreSettings)


def database_settings(settings: Settings) -> DatabaseSettings:
    """The validated `database` section of loaded settings."""
    return settings.section(DATABASE_SECTION, DatabaseSettings)


def store_settings(settings: Settings) -> StoreSettings:
    """The validated `store` section of loaded settings."""
    return settings.section(STORE_SECTION, StoreSettings)
