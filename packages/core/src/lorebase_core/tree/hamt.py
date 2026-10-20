"""Partitioned canonical HAMT: lookups, batch updates and iteration (design 6.2).

Inside a partition an entry ``(element_key, object_hash)`` is placed by
``h = sha256(element_key)``. A node at depth ``d`` is a leaf iff its subtree
holds at most ``L`` entries; otherwise it is a branch whose children are
selected by ``h[d]``. The shape depends only on the entry set, so equal sets
always produce equal root hashes.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import NamedTuple

from lorebase_core.canonical import HASH_SIZE, node_hash
from lorebase_core.tree.node import (
    BranchNode,
    LeafNode,
    NodeFormatError,
    PartitionRef,
    encode_node,
    key_hash,
)
from lorebase_core.tree.params import TreeParams
from lorebase_core.tree.root import Tree, write_root
from lorebase_core.tree.store import NodeStore, read_node

__all__ = [
    "TreeChange",
    "empty_tree",
    "iter_partition",
    "tree_apply",
    "tree_count",
    "tree_entries",
    "tree_get",
]

_DEFAULT_PARAMS = TreeParams()


class TreeChange(NamedTuple):
    """Set ``key`` in ``partition`` to ``new_hash``; ``None`` removes it."""

    partition: str
    key: str
    new_hash: bytes | None


# (key hash, element key, object hash), ordered by key hash.
_Entry = tuple[bytes, str, bytes]
# (key hash, element key, new object hash or None for removal), ordered by key hash.
_Change = tuple[bytes, str, bytes | None]


@dataclass(slots=True)
class _Small:
    """A subtree small enough to be a leaf, not yet written. ``origin`` is its unchanged node."""

    entries: list[_Entry]
    origin: bytes | None = None

    @property
    def count(self) -> int:
        return len(self.entries)


@dataclass(slots=True)
class _Large:
    """A subtree over the leaf threshold, already written as a branch node."""

    node: bytes
    count: int


_Subtree = _Small | _Large


def empty_tree(store: NodeStore) -> bytes:
    """Store the empty tree root and return its hash."""
    return write_root(store, {})


def tree_get(store: NodeStore, root: bytes | None, partition: str, key: str) -> bytes | None:
    """Return the object hash stored under ``key`` in ``partition``, or ``None``."""
    if root is None:
        return None
    ref = Tree.load(store, root).partitions.get(partition)
    if ref is None:
        return None
    target = key_hash(key)
    current = ref.root
    depth = 0
    while True:
        node = read_node(store, current)
        _check_partition_node(node, depth)
        if isinstance(node, LeafNode):
            for entry_key, value in node.entries:
                if entry_key == key:
                    return value
            return None
        assert isinstance(node, BranchNode)
        child = _child(node, target[depth])
        if child is None:
            return None
        current = child
        depth += 1


def tree_count(store: NodeStore, root: bytes | None, partition: str) -> int:
    """Number of entries in ``partition``, read from the root in O(1)."""
    if root is None:
        return 0
    ref = Tree.load(store, root).partitions.get(partition)
    return 0 if ref is None else ref.count


def tree_entries(
    store: NodeStore, root: bytes | None, partition: str
) -> Iterator[tuple[str, bytes]]:
    """Iterate ``(element_key, object_hash)`` of ``partition`` in key-hash order."""
    if root is None:
        return
    ref = Tree.load(store, root).partitions.get(partition)
    if ref is None:
        return
    for _, key, value in iter_partition(store, ref.root, 0):
        yield key, value


def iter_partition(store: NodeStore, node: bytes, depth: int) -> Iterator[_Entry]:
    """Iterate ``(key_hash, key, object_hash)`` of the subtree at ``node`` in key-hash order."""
    decoded = read_node(store, node)
    _check_partition_node(decoded, depth)
    if isinstance(decoded, LeafNode):
        for key, value in decoded.entries:
            yield key_hash(key), key, value
        return
    assert isinstance(decoded, BranchNode)
    for _, child in decoded.children:
        yield from iter_partition(store, child, depth + 1)


def tree_apply(
    store: NodeStore,
    root: bytes | None,
    changes: Iterable[TreeChange | tuple[str, str, bytes | None]],
    params: TreeParams = _DEFAULT_PARAMS,
) -> bytes:
    """Apply a batch of changes by path copying and return the new root hash.

    ``root=None`` stands for the empty tree. New nodes are written to ``store``;
    unchanged subtrees are shared. Removing an absent key or setting a key to
    its current hash is a no-op. A key may appear at most once per batch.
    """
    grouped = _group_changes(changes)
    if root is None:
        partitions: dict[str, PartitionRef] = {}
    else:
        partitions = dict(Tree.load(store, root).partitions)
        if not grouped:
            return root
    touched = False
    for partition, partition_changes in grouped.items():
        ref = partitions.get(partition)
        subtree, _ = _apply(store, None if ref is None else ref.root, 0, partition_changes, params)
        new_root = _materialize(store, subtree, 0)
        if new_root is None:
            touched |= partitions.pop(partition, None) is not None
            continue
        new_ref = PartitionRef(new_root, subtree.count)
        if new_ref != ref:
            partitions[partition] = new_ref
            touched = True
    if root is not None and not touched:
        return root
    return write_root(store, partitions)


def _group_changes(
    changes: Iterable[TreeChange | tuple[str, str, bytes | None]],
) -> dict[str, list[_Change]]:
    grouped: dict[str, dict[str, bytes | None]] = {}
    for partition, key, new_hash in changes:
        if not isinstance(partition, str) or not partition:
            raise ValueError("partition ids are non-empty strings")
        if not isinstance(key, str):
            raise TypeError("element keys are strings")
        if new_hash is not None and len(new_hash) != HASH_SIZE:
            raise ValueError(f"object hashes are {HASH_SIZE} bytes")
        keys = grouped.setdefault(partition, {})
        if key in keys:
            raise ValueError(f"duplicate change for key {key!r} in partition {partition!r}")
        keys[key] = new_hash
    return {
        partition: sorted((key_hash(k), k, v) for k, v in keys.items())
        for partition, keys in grouped.items()
    }


def _apply(
    store: NodeStore,
    node: bytes | None,
    depth: int,
    changes: list[_Change],
    params: TreeParams,
) -> tuple[_Subtree, int]:
    """Apply sorted ``changes`` to the subtree at ``node``; return it and its old count."""
    if node is None:
        return _finish(store, _merge([], changes), depth, params), 0
    decoded = read_node(store, node)
    _check_partition_node(decoded, depth)
    if isinstance(decoded, LeafNode):
        entries = [(key_hash(k), k, v) for k, v in decoded.entries]
        merged = _merge(entries, changes)
        if merged == entries:
            return _Small(entries, origin=node), decoded.count
        return _finish(store, merged, depth, params), decoded.count
    assert isinstance(decoded, BranchNode)
    children = dict(decoded.children)
    results: dict[int, _Subtree] = {}
    old_total = new_total = 0
    for index, group in _by_byte(changes, depth).items():
        subtree, old = _apply(store, children.get(index), depth + 1, group, params)
        results[index] = subtree
        old_total += old
        new_total += subtree.count
    count = decoded.count - old_total + new_total
    if count > params.leaf_size:
        changed = False
        for index, subtree in results.items():
            child = _materialize(store, subtree, depth + 1)
            if child == children.get(index):
                continue
            changed = True
            if child is None:
                del children[index]
            else:
                children[index] = child
        if not changed:
            return _Large(node, count), decoded.count
        return _Large(_write_branch(store, depth, children, count), count), decoded.count
    # The subtree shrank to the leaf threshold: collapse it into one leaf.
    entries = []
    for index in sorted(children.keys() | results.keys()):
        subtree_result = results.get(index)
        if subtree_result is None:
            entries.extend(iter_partition(store, children[index], depth + 1))
        else:
            assert isinstance(subtree_result, _Small)
            entries.extend(subtree_result.entries)
    return _Small(entries), decoded.count


def _merge(entries: list[_Entry], changes: list[_Change]) -> list[_Entry]:
    """Merge sorted changes into sorted entries."""
    result: list[_Entry] = []
    i = 0
    for change_hash, change_key, new_value in changes:
        while i < len(entries) and entries[i][0] < change_hash:
            result.append(entries[i])
            i += 1
        if i < len(entries) and entries[i][0] == change_hash:
            if entries[i][1] != change_key:
                raise NodeFormatError("distinct keys with equal key hash")
            i += 1
        if new_value is not None:
            result.append((change_hash, change_key, new_value))
    result.extend(entries[i:])
    return result


def _finish(store: NodeStore, entries: list[_Entry], depth: int, params: TreeParams) -> _Subtree:
    if len(entries) <= params.leaf_size:
        return _Small(entries)
    return _Large(_build(store, entries, depth, params), len(entries))


def _build(store: NodeStore, entries: list[_Entry], depth: int, params: TreeParams) -> bytes:
    """Write the canonical subtree for more than ``L`` sorted entries; return its hash."""
    if depth >= HASH_SIZE:
        raise NodeFormatError("more entries than the leaf size share a full key hash")
    children: dict[int, bytes] = {}
    for index, group in _by_byte(entries, depth).items():
        if len(group) <= params.leaf_size:
            children[index] = _write_leaf(store, depth + 1, group)
        else:
            children[index] = _build(store, group, depth + 1, params)
    return _write_branch(store, depth, children, len(entries))


def _materialize(store: NodeStore, subtree: _Subtree, depth: int) -> bytes | None:
    """Return the node hash of a subtree result, writing a leaf when needed."""
    if isinstance(subtree, _Large):
        return subtree.node
    if not subtree.entries:
        return None
    if subtree.origin is not None:
        return subtree.origin
    return _write_leaf(store, depth, subtree.entries)


def _write_leaf(store: NodeStore, depth: int, entries: list[_Entry]) -> bytes:
    body = encode_node(LeafNode(depth, tuple((key, value) for _, key, value in entries)))
    digest = node_hash(body)
    store.put(digest, body)
    return digest


def _write_branch(store: NodeStore, depth: int, children: dict[int, bytes], count: int) -> bytes:
    body = encode_node(BranchNode(depth, tuple(sorted(children.items())), count))
    digest = node_hash(body)
    store.put(digest, body)
    return digest


def _by_byte[T: tuple[bytes, str, bytes | None]](items: list[T], depth: int) -> dict[int, list[T]]:
    groups: dict[int, list[T]] = {}
    for item in items:
        groups.setdefault(item[0][depth], []).append(item)
    return groups


def _child(node: BranchNode, index: int) -> bytes | None:
    for child_index, child in node.children:
        if child_index == index:
            return child
    return None


def _check_partition_node(node: object, depth: int) -> None:
    if not isinstance(node, LeafNode | BranchNode) or node.depth != depth:
        raise NodeFormatError(f"expected a partition node at depth {depth}")
