"""Reads from a Revision tree (time travel, design 6.12).

Key lookups walk the trie (prefetched path, then a cached node read per level); listings scan
the whole Kind or Relation partition, sort its keys and page through them, so their cost grows
with the partition. Partitions above `read.time_travel.max_scan_entries` are refused.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from lorebase_core.tree import Tree, tree_get
from lorebase_keeper.errors import VALIDATION_FAILED, ProblemError
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_store.nodestore import NodeRepository, PgNodeStore
from lorebase_store.objectstore import PgObjectStore


@dataclass(frozen=True, slots=True)
class TreeEntry:
    key: str
    object_hash: bytes


class TreeReader:
    """Reads of one Revision tree in one Domain."""

    def __init__(
        self, session: AsyncSession, runtime: ReadRuntime, domain_id: str, root: bytes
    ) -> None:
        settings = runtime.store_settings
        self._session = session
        self._runtime = runtime
        self._domain = domain_id
        self._root = root
        self._nodes = NodeRepository(
            session,
            domain_id,
            writes=settings.writes,
            cache=runtime.store.node_cache,
            batch=settings.nodes.prefetch_batch,
        )
        self._objects = PgObjectStore(
            session, domain_id, writes=settings.writes, batch=settings.materialize.read_batch_rows
        )
        self._tree: Tree | None = None

    async def tree(self) -> Tree:
        if self._tree is None:
            self._tree = await self._nodes.load_tree(self._root)
        return self._tree

    async def count(self, partition: str) -> int:
        ref = (await self.tree()).partitions.get(partition)
        return 0 if ref is None else ref.count

    async def get(self, partition: str, key: str) -> bytes | None:
        """Object hash stored under `key`, or None."""
        settings = self._runtime.store_settings
        cache = self._runtime.store.node_cache
        domain, root = self._domain, self._root

        def lookup(sync: Session) -> bytes | None:
            store = PgNodeStore(
                sync, domain, cache=cache, prefetch_batch=settings.nodes.prefetch_batch
            )
            store.prefetch_paths(root, [(partition, key)])
            return tree_get(store, root, partition, key)

        return await self._session.run_sync(lookup)

    async def entries(self, partition: str) -> list[TreeEntry]:
        """Every entry of a partition sorted by key; refused above the configured size."""
        limit = self._runtime.read.time_travel.max_scan_entries
        ref = (await self.tree()).partitions.get(partition)
        if ref is None:
            return []
        if ref.count > limit:
            raise ProblemError(
                VALIDATION_FAILED,
                f"Listing at a Revision scans the whole partition; this one holds {ref.count} "
                f"entries, more than the limit of {limit}. Read the materialized branch instead.",
            )
        found: list[TreeEntry] = []
        async for batch in self._nodes.scan(ref.root):
            found.extend(TreeEntry(key, digest) for key, digest in batch)
        found.sort(key=lambda entry: entry.key)
        return found

    async def bodies(self, hashes: Iterable[bytes]) -> dict[bytes, Mapping[str, Any]]:
        """Stored object bodies by hash."""
        stored = await self._objects.get_many(hashes)
        return {digest: body.body for digest, body in stored.items()}
