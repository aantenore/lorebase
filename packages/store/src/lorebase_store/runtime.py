"""Process-wide store state: the `store` settings and the tree node cache."""

from __future__ import annotations

from dataclasses import dataclass

from lorebase_platform.settings import load_settings
from lorebase_store.nodestore import NodeCache
from lorebase_store.settings import StoreSettings, store_settings


@dataclass(frozen=True, slots=True)
class StoreRuntime:
    """Settings and caches shared by the commits and reads of one process."""

    settings: StoreSettings
    node_cache: NodeCache

    @classmethod
    def from_settings(cls, settings: StoreSettings) -> StoreRuntime:
        return cls(settings=settings, node_cache=NodeCache.from_settings(settings.nodes))


_default: StoreRuntime | None = None


def default_runtime() -> StoreRuntime:
    """Runtime built once from the loaded configuration (`load_settings()`)."""
    global _default
    if _default is None:
        _default = StoreRuntime.from_settings(store_settings(load_settings()))
    return _default
