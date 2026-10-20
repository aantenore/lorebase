"""Tree nodes in Postgres (`tree_nodes`), scoped to one Domain.

Two access paths share one process-wide cache:

- `PgNodeStore` implements the synchronous core `NodeStore` protocol over a synchronous
  `Session`. It is meant to run inside `AsyncSession.run_sync`, where SQLAlchemy drives the
  async driver from synchronous code. Callers prefetch the nodes a tree operation will read
  (`prefetch_paths` for a batch of keys) so that the operation reads from memory; a miss falls
  back to a single-node query. Writes are buffered and handed to the async side in bulk.
- `NodeRepository` is the async side: batched reads, bulk writes with `ON CONFLICT DO NOTHING`,
  and streaming scans of whole partitions.

Nodes are immutable and content-addressed, so cached bodies never go stale. The cache only
holds bodies read from the database; `has()` never answers from it, because a node written by
a transaction that later rolled back may still be cached.
"""

from __future__ import annotations

import threading
from collections import OrderedDict, defaultdict
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass

from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from lorebase_core.canonical import node_hash
from lorebase_core.tree import (
    BranchNode,
    LeafNode,
    MissingNodeError,
    NodeType,
    Tree,
    decode_node,
    key_hash,
)
from lorebase_store.bulk import bulk_write, chunks
from lorebase_store.models import metadata
from lorebase_store.settings import NodeSettings, WriteSettings

TREE_NODES: Table = metadata.tables["tree_nodes"]

_NODE_TYPE_OFFSET = 1
"""Byte offset of the node type in the node header (after the format version)."""


def node_type(body: bytes) -> NodeType:
    """Type of an encoded node, read from its header without decoding the body."""
    return NodeType(body[_NODE_TYPE_OFFSET])


class NodeCache:
    """Thread-safe LRU of node bodies keyed by `(domain_id, hash)`, bounded in bytes."""

    def __init__(self, max_bytes: int) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        self._max_bytes = max_bytes
        self._bytes = 0
        self._entries: OrderedDict[tuple[str, bytes], bytes] = OrderedDict()
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: NodeSettings) -> NodeCache:
        return cls(settings.cache_bytes)

    @property
    def size_bytes(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, domain_id: str, digest: bytes) -> bytes | None:
        key = (domain_id, digest)
        with self._lock:
            body = self._entries.get(key)
            if body is not None:
                self._entries.move_to_end(key)
            return body

    def put(self, domain_id: str, digest: bytes, body: bytes) -> None:
        if len(body) > self._max_bytes:
            return
        key = (domain_id, digest)
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                return
            self._entries[key] = body
            self._bytes += len(body)
            while self._bytes > self._max_bytes:
                _, evicted = self._entries.popitem(last=False)
                self._bytes -= len(evicted)


@dataclass
class NodeReadStats:
    """Counters of a `PgNodeStore`, used to check that reads are batched."""

    queries: int = 0
    fetched: int = 0
    misses: int = 0


class PgNodeStore:
    """Synchronous `NodeStore` for one Domain over a synchronous `Session` (see module doc)."""

    def __init__(
        self,
        session: Session,
        domain_id: str,
        *,
        cache: NodeCache | None = None,
        prefetch_batch: int = 1000,
    ) -> None:
        self._session = session
        self._domain = domain_id
        self._cache = cache
        self._batch = prefetch_batch
        self._local: dict[bytes, bytes] = {}
        self._pending: dict[bytes, bytes] = {}
        self.stats = NodeReadStats()

    @property
    def domain_id(self) -> str:
        return self._domain

    # NodeStore protocol ---------------------------------------------------------------

    def get(self, node_hash: bytes) -> bytes | None:
        body = self._known(node_hash)
        if body is None:
            self.stats.misses += 1
            self.prefetch([node_hash])
            body = self._local.get(node_hash)
        return body

    def put(self, node_hash: bytes, body: bytes) -> None:
        _verify(node_hash, body)
        self._pending.setdefault(node_hash, body)

    def has(self, node_hash: bytes) -> bool:
        if node_hash in self._pending or node_hash in self._local:
            return True
        self.prefetch([node_hash], use_cache=False)
        return node_hash in self._local

    # Batched reads ------------------------------------------------------------------

    def prefetch(self, hashes: Iterable[bytes], *, use_cache: bool = True) -> None:
        """Load the given nodes in batched queries; already known nodes are skipped."""
        missing: list[bytes] = []
        for digest in dict.fromkeys(hashes):
            if digest in self._pending or digest in self._local:
                continue
            cached = self._cache.get(self._domain, digest) if use_cache and self._cache else None
            if cached is not None:
                self._local[digest] = cached
            else:
                missing.append(digest)
        for chunk in chunks(missing, self._batch):
            rows = self._session.execute(
                select(TREE_NODES.c.hash, TREE_NODES.c.body).where(
                    TREE_NODES.c.domain_id == self._domain, TREE_NODES.c.hash.in_(chunk)
                )
            )
            self.stats.queries += 1
            for digest, body in rows:
                self._local[digest] = body
                self.stats.fetched += 1
                if self._cache is not None:
                    self._cache.put(self._domain, digest, body)

    def prefetch_paths(self, root: bytes | None, keys: Iterable[tuple[str, str]]) -> None:
        """Load the nodes on the paths from `root` to the given `(partition, key)` entries.

        One query per tree level, so a batch update over the prefetched paths reads no node
        individually (except when a branch collapses into a leaf and siblings are read).
        """
        if root is None:
            return
        self.prefetch([root])
        if self._known(root) is None:
            raise MissingNodeError(root.hex())
        partitions = Tree.load(self, root).partitions
        wanted: dict[str, list[bytes]] = defaultdict(list)
        for partition, key in keys:
            if partition in partitions:
                wanted[partition].append(key_hash(key))
        frontier = [(partitions[p].root, 0, hashes) for p, hashes in wanted.items()]
        while frontier:
            self.prefetch(digest for digest, _, _ in frontier)
            following: list[tuple[bytes, int, list[bytes]]] = []
            for digest, depth, hashes in frontier:
                body = self._known(digest)
                if body is None:
                    raise MissingNodeError(digest.hex())
                if node_type(body) is not NodeType.BRANCH:
                    continue
                node = decode_node(body)
                assert isinstance(node, BranchNode)
                children = dict(node.children)
                groups: dict[int, list[bytes]] = defaultdict(list)
                for placement in hashes:
                    groups[placement[depth]].append(placement)
                for index, group in groups.items():
                    child = children.get(index)
                    if child is not None:
                        following.append((child, depth + 1, group))
            frontier = following

    # Buffered writes ------------------------------------------------------------------

    @property
    def pending(self) -> Mapping[bytes, bytes]:
        """Nodes written through this store and not yet persisted."""
        return self._pending

    def take_pending(self) -> dict[bytes, bytes]:
        """Return and forget the buffered writes, to persist with `NodeRepository.put_many`."""
        pending, self._pending = self._pending, {}
        self._local.update(pending)
        return pending

    def _known(self, digest: bytes) -> bytes | None:
        body = self._pending.get(digest) or self._local.get(digest)
        if body is None and self._cache is not None:
            body = self._cache.get(self._domain, digest)
            if body is not None:
                self._local[digest] = body
        return body


class NodeRepository:
    """Async batched reads and bulk writes of one Domain's tree nodes."""

    def __init__(
        self,
        session: AsyncSession,
        domain_id: str,
        *,
        writes: WriteSettings,
        cache: NodeCache | None = None,
        batch: int = 1000,
    ) -> None:
        self._session = session
        self._domain = domain_id
        self._writes = writes
        self._cache = cache
        self._batch = batch

    async def get_many(
        self, hashes: Iterable[bytes], *, use_cache: bool = True
    ) -> dict[bytes, bytes]:
        """Bodies of the given nodes that exist."""
        found: dict[bytes, bytes] = {}
        missing: list[bytes] = []
        for digest in dict.fromkeys(hashes):
            cached = self._cache.get(self._domain, digest) if use_cache and self._cache else None
            if cached is None:
                missing.append(digest)
            else:
                found[digest] = cached
        for chunk in chunks(missing, self._batch):
            rows = await self._session.execute(
                select(TREE_NODES.c.hash, TREE_NODES.c.body).where(
                    TREE_NODES.c.domain_id == self._domain, TREE_NODES.c.hash.in_(chunk)
                )
            )
            for digest, body in rows:
                found[digest] = body
                if use_cache and self._cache is not None:
                    self._cache.put(self._domain, digest, body)
        return found

    async def put_many(self, nodes: Mapping[bytes, bytes]) -> None:
        """Persist nodes; nodes already stored are left unchanged."""
        rows = [
            {"domain_id": self._domain, "hash": digest, "body": body}
            for digest, body in nodes.items()
        ]
        await bulk_write(self._session, TREE_NODES, rows, writes=self._writes)

    async def load_tree(self, root: bytes) -> Tree:
        """The partition map of the tree rooted at `root`."""
        body = (await self.get_many([root])).get(root)
        if body is None:
            raise MissingNodeError(root.hex())
        return Tree.load(_SingleNode(root, body), root)

    async def scan(self, node: bytes) -> AsyncIterator[list[tuple[str, bytes]]]:
        """Stream the `(element_key, object_hash)` entries of a partition subtree.

        Reads one tree level per round in batched queries and yields the entries of each
        batch of leaves; memory stays bounded by one batch plus the branch frontier. Scanned
        nodes do not enter the cache.
        """
        frontier: list[bytes] = [node]
        while frontier:
            following: list[bytes] = []
            for chunk in chunks(frontier, self._batch):
                bodies = await self.get_many(chunk, use_cache=False)
                entries: list[tuple[str, bytes]] = []
                for digest in chunk:
                    body = bodies.get(digest)
                    if body is None:
                        raise MissingNodeError(digest.hex())
                    decoded = decode_node(body)
                    if isinstance(decoded, LeafNode):
                        entries.extend(decoded.entries)
                    elif isinstance(decoded, BranchNode):
                        following.extend(child for _, child in decoded.children)
                if entries:
                    yield entries
            frontier = following


class _SingleNode:
    """Read-only `NodeStore` over one known node."""

    def __init__(self, digest: bytes, body: bytes) -> None:
        self._digest = digest
        self._body = body

    def get(self, node_hash: bytes) -> bytes | None:
        return self._body if node_hash == self._digest else None

    def put(self, node_hash: bytes, body: bytes) -> None:
        raise TypeError("read-only node store")

    def has(self, node_hash: bytes) -> bool:
        return node_hash == self._digest


def _verify(expected: bytes, body: bytes) -> None:
    if node_hash(body) != expected:
        raise ValueError("node hash does not match node body")
