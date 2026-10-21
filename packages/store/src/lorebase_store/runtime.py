"""Process-wide store state: the `store` settings, the tree node cache and the schema cache."""

from __future__ import annotations

from dataclasses import dataclass

from lorebase_platform.settings import load_settings
from lorebase_store.nodestore import NodeCache
from lorebase_store.schema_cache import SchemaCache
from lorebase_store.settings import (
    DatabaseSettings,
    StoreSettings,
    database_settings,
    store_settings,
)


@dataclass(frozen=True, slots=True)
class StoreRuntime:
    """Settings and caches shared by the commits and reads of one process.

    `index_maintenance` is true when `database.maintenance_dsn` is set: commits by a role
    that does not own `mat_records` then queue their per-Attribute index changes for the job
    worker, which runs them as the maintenance role (`lorebase_store.indexes`).
    """

    settings: StoreSettings
    node_cache: NodeCache
    schema_cache: SchemaCache
    index_maintenance: bool = False

    @classmethod
    def from_settings(
        cls, settings: StoreSettings, *, database: DatabaseSettings | None = None
    ) -> StoreRuntime:
        return cls(
            settings=settings,
            node_cache=NodeCache.from_settings(settings.nodes),
            schema_cache=SchemaCache(settings.reads.schema_cache_entries),
            index_maintenance=database is not None and database.maintenance_dsn is not None,
        )


_default: StoreRuntime | None = None


def default_runtime() -> StoreRuntime:
    """Runtime built once from the loaded configuration (`load_settings()`)."""
    global _default
    if _default is None:
        loaded = load_settings()
        _default = StoreRuntime.from_settings(
            store_settings(loaded), database=database_settings(loaded)
        )
    return _default
