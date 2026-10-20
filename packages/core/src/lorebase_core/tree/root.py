"""Revision tree root: the partition map (design 6.2)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from lorebase_core.canonical import node_hash
from lorebase_core.tree.node import NodeFormatError, PartitionRef, RootNode, encode_node
from lorebase_core.tree.store import NodeStore, read_node

__all__ = [
    "LINK_KEY_SEPARATOR",
    "PARTITION_SCHEMA",
    "Tree",
    "kind_partition",
    "link_key",
    "relation_partition",
    "write_root",
]

PARTITION_SCHEMA = "schema"
LINK_KEY_SEPARATOR = "\x1f"
_KIND_PREFIX = "k:"
_RELATION_PREFIX = "r:"


def kind_partition(kind_id: str) -> str:
    """Partition id holding the Records of a Kind, keyed by Record key."""
    return _KIND_PREFIX + kind_id


def relation_partition(relation_id: str) -> str:
    """Partition id holding the Links of a Relation, keyed by :func:`link_key`."""
    return _RELATION_PREFIX + relation_id


def link_key(from_key: str, to_key: str) -> str:
    """Element key of a Link inside its Relation partition."""
    return from_key + LINK_KEY_SEPARATOR + to_key


@dataclass(frozen=True, eq=False)
class Tree:
    """A Revision tree: its root hash and the partition map it points to."""

    root: bytes
    partitions: Mapping[str, PartitionRef]

    @classmethod
    def load(cls, store: NodeStore, root: bytes) -> Tree:
        node = read_node(store, root)
        if not isinstance(node, RootNode):
            raise NodeFormatError("hash does not reference a tree root")
        return cls(root, MappingProxyType(dict(node.partitions)))

    @property
    def count(self) -> int:
        """Total number of entries across partitions."""
        return sum(ref.count for ref in self.partitions.values())


def write_root(store: NodeStore, partitions: Mapping[str, PartitionRef]) -> bytes:
    """Encode and store a root node for ``partitions``; return its hash."""
    ordered = sorted(partitions.items(), key=lambda item: item[0].encode("utf-8"))
    body = encode_node(RootNode(tuple(ordered)))
    root = node_hash(body)
    store.put(root, body)
    return root
