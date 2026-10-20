"""Node storage protocol and in-memory implementations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from lorebase_core.canonical import node_hash
from lorebase_core.tree.node import Node, decode_node

__all__ = [
    "InMemoryNodeStore",
    "MemoryNodeStore",
    "MissingNodeError",
    "NodeStore",
    "StagingNodeStore",
    "read_node",
]


class MissingNodeError(KeyError):
    """Raised when a node referenced by a tree is not in the store."""


@runtime_checkable
class NodeStore(Protocol):
    """Content-addressed storage of encoded tree nodes, keyed by node hash."""

    def get(self, node_hash: bytes) -> bytes | None: ...

    def put(self, node_hash: bytes, body: bytes) -> None: ...

    def has(self, node_hash: bytes) -> bool: ...


class InMemoryNodeStore:
    """Dictionary-backed :class:`NodeStore` that verifies hashes on write."""

    def __init__(self) -> None:
        self._nodes: dict[bytes, bytes] = {}

    def get(self, node_hash: bytes) -> bytes | None:
        return self._nodes.get(node_hash)

    def put(self, node_hash: bytes, body: bytes) -> None:
        _verify(node_hash, body)
        self._nodes[node_hash] = body

    def has(self, node_hash: bytes) -> bool:
        return node_hash in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)


MemoryNodeStore = InMemoryNodeStore


class StagingNodeStore:
    """Reads through to ``base`` and keeps new writes aside in :attr:`written`.

    Lets a caller collect the nodes produced by a batch update and persist them
    in bulk, or drop them.
    """

    def __init__(self, base: NodeStore) -> None:
        self._base = base
        self._written: dict[bytes, bytes] = {}

    @property
    def written(self) -> Mapping[bytes, bytes]:
        """Nodes written through this store that the base store does not hold."""
        return self._written

    def get(self, node_hash: bytes) -> bytes | None:
        body = self._written.get(node_hash)
        return body if body is not None else self._base.get(node_hash)

    def put(self, node_hash: bytes, body: bytes) -> None:
        _verify(node_hash, body)
        if node_hash not in self._written and not self._base.has(node_hash):
            self._written[node_hash] = body

    def has(self, node_hash: bytes) -> bool:
        return node_hash in self._written or self._base.has(node_hash)


def read_node(store: NodeStore, node_hash: bytes) -> Node:
    """Fetch and decode a node, raising :class:`MissingNodeError` when absent."""
    body = store.get(node_hash)
    if body is None:
        raise MissingNodeError(node_hash.hex())
    return decode_node(body)


def _verify(expected: bytes, body: bytes) -> None:
    if node_hash(body) != expected:
        raise ValueError("node hash does not match node body")
