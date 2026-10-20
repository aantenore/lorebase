"""Tree diff by simultaneous walk, skipping equal subtrees (design 6.7)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import NamedTuple

from lorebase_core.tree.node import (
    BranchNode,
    LeafNode,
    NodeFormatError,
    PartitionRef,
    key_hash,
)
from lorebase_core.tree.root import Tree
from lorebase_core.tree.store import NodeStore, read_node

__all__ = ["DiffEntry", "tree_diff"]


class DiffEntry(NamedTuple):
    """One differing key: ``hash_a`` is its value in tree A, ``hash_b`` in tree B (``None`` = absent)."""

    partition: str
    key: str
    hash_a: bytes | None
    hash_b: bytes | None


_Entry = tuple[bytes, str, bytes]
# A side of the walk: a stored node, entries already in memory, or nothing.
_Side = bytes | list[_Entry] | None


def tree_diff(store: NodeStore, root_a: bytes | None, root_b: bytes | None) -> Iterator[DiffEntry]:
    """Stream the differences between two trees.

    Partitions and subtrees with equal hashes are skipped without being read,
    so the cost is proportional to the differing leaves. Output is ordered by
    partition id (UTF-8 bytes), then by key hash. ``None`` stands for the empty tree.
    """
    if root_a == root_b:
        return
    partitions_a = _partitions(store, root_a)
    partitions_b = _partitions(store, root_b)
    for partition in sorted(partitions_a.keys() | partitions_b.keys(), key=_utf8):
        ref_a = partitions_a.get(partition)
        ref_b = partitions_b.get(partition)
        node_a = None if ref_a is None else ref_a.root
        node_b = None if ref_b is None else ref_b.root
        if node_a != node_b:
            yield from _diff(store, partition, node_a, node_b, 0)


def _partitions(store: NodeStore, root: bytes | None) -> dict[str, PartitionRef]:
    return {} if root is None else dict(Tree.load(store, root).partitions)


def _diff(store: NodeStore, partition: str, a: _Side, b: _Side, depth: int) -> Iterator[DiffEntry]:
    if isinstance(a, bytes) and a == b:
        return
    children_a = _expand(store, a, depth)
    children_b = _expand(store, b, depth)
    if isinstance(children_a, list) and isinstance(children_b, list):
        yield from _diff_entries(partition, children_a, children_b)
        return
    buckets_a = _buckets(children_a, depth)
    buckets_b = _buckets(children_b, depth)
    for index in sorted(buckets_a.keys() | buckets_b.keys()):
        yield from _diff(store, partition, buckets_a.get(index), buckets_b.get(index), depth + 1)


def _expand(store: NodeStore, side: _Side, depth: int) -> dict[int, bytes] | list[_Entry]:
    """Load a side: a branch becomes its children by byte, a leaf its entries."""
    if side is None:
        return []
    if isinstance(side, list):
        return side
    node = read_node(store, side)
    if isinstance(node, LeafNode) and node.depth == depth:
        return [(key_hash(key), key, value) for key, value in node.entries]
    if isinstance(node, BranchNode) and node.depth == depth:
        return dict(node.children)
    raise NodeFormatError(f"expected a partition node at depth {depth}")


def _buckets(side: dict[int, bytes] | list[_Entry], depth: int) -> dict[int, _Side]:
    buckets: dict[int, _Side] = {}
    if isinstance(side, dict):
        buckets.update(side)
        return buckets
    for entry in side:
        bucket = buckets.setdefault(entry[0][depth], [])
        assert isinstance(bucket, list)
        bucket.append(entry)
    return buckets


def _diff_entries(partition: str, a: list[_Entry], b: list[_Entry]) -> Iterator[DiffEntry]:
    i = j = 0
    while i < len(a) or j < len(b):
        if j == len(b) or (i < len(a) and a[i][0] < b[j][0]):
            yield DiffEntry(partition, a[i][1], a[i][2], None)
            i += 1
        elif i == len(a) or b[j][0] < a[i][0]:
            yield DiffEntry(partition, b[j][1], None, b[j][2])
            j += 1
        else:
            if a[i][2] != b[j][2]:
                yield DiffEntry(partition, a[i][1], a[i][2], b[j][2])
            i += 1
            j += 1


def _utf8(text: str) -> bytes:
    return text.encode("utf-8")
