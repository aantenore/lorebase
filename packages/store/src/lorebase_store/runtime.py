"""Process-wide store state: the `store` and `schema` settings, the tree node cache and the
schema cache."""

from __future__ import annotations

from dataclasses import dataclass, field

from lorebase_platform.settings import Settings, load_settings
from lorebase_store.digests import DigestKeys
from lorebase_store.nodestore import NodeCache
from lorebase_store.schema_cache import SchemaCache
from lorebase_store.settings import (
    DatabaseSettings,
    SchemaSettings,
    StoreSettings,
    database_settings,
    schema_settings,
    store_settings,
)
from lorebase_store.vault.keys import VaultKeys
from lorebase_store.vault.settings import VaultSettings, vault_settings


@dataclass(frozen=True, slots=True)
class StoreRuntime:
    """Settings and caches shared by the commits and reads of one process.

    `vault` holds the keys of the personal data vault (`vault` settings), None when it is not
    configured; `vault_settings` the whole section; `digests` the keys of the keyed digests
    (`lorebase_store.digests`). `index_maintenance` is true when `database.maintenance_dsn` is
    set: commits by a role that does not own `mat_records` then queue their per-Attribute index
    changes for the job worker, which runs them as the maintenance role
    (`lorebase_store.indexes`).
    """

    settings: StoreSettings
    node_cache: NodeCache
    schema_cache: SchemaCache
    schema: SchemaSettings = field(default_factory=SchemaSettings)
    index_maintenance: bool = False
    vault: VaultKeys | None = None
    vault_settings: VaultSettings = field(default_factory=VaultSettings)
    digests: DigestKeys = field(default_factory=DigestKeys)

    @classmethod
    def from_settings(
        cls,
        settings: StoreSettings,
        schema: SchemaSettings | None = None,
        *,
        database: DatabaseSettings | None = None,
        vault: VaultSettings | None = None,
    ) -> StoreRuntime:
        keys = None if vault is None else VaultKeys.from_settings(vault)
        vault = vault or VaultSettings()
        return cls(
            settings=settings,
            node_cache=NodeCache.from_settings(settings.nodes),
            schema_cache=SchemaCache(settings.reads.schema_cache_entries),
            schema=schema or SchemaSettings(),
            index_maintenance=database is not None and database.maintenance_dsn is not None,
            vault=keys,
            vault_settings=vault,
            digests=DigestKeys.from_settings(vault, keys),
        )

    @classmethod
    def from_loaded(cls, settings: Settings) -> StoreRuntime:
        """Runtime of loaded settings: the `store`, `schema`, `database` and `vault`
        sections."""
        return cls.from_settings(
            store_settings(settings),
            schema_settings(settings),
            database=database_settings(settings),
            vault=vault_settings(settings),
        )


_default: StoreRuntime | None = None


def default_runtime() -> StoreRuntime:
    """Runtime built once from the loaded configuration (`load_settings()`)."""
    global _default
    if _default is None:
        _default = StoreRuntime.from_loaded(load_settings())
    return _default
