"""Compact binary encoding of tree nodes (design 6.2).

Every node starts with a header::

    format version (u8) | node type (u8) | depth (u8) | entry count (varint) | subtree count (varint)

followed by a type-specific body:

- leaf: per entry ``varint(len(key)) | key utf-8 | object hash``, sorted by ``sha256(key)``;
- branch: a 256-bit occupancy bitmap (bit ``i`` set when the child for key-hash
  byte ``i`` exists, most significant bit first) and the child hashes in byte order;
- root: per partition ``varint(len(id)) | id utf-8 | partition root hash | varint(count)``,
  sorted by the UTF-8 bytes of the partition id.

Varints are unsigned LEB128. Decoding accepts only the canonical encoding, so a
node has exactly one byte representation and therefore exactly one hash.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import IntEnum

from lorebase_core.canonical import HASH_SIZE, node_hash
from lorebase_core.tree.params import BITS_PER_BYTE, FANOUT, NODE_FORMAT_VERSION

__all__ = [
    "BranchNode",
    "LeafNode",
    "Node",
    "NodeFormatError",
    "NodeType",
    "PartitionRef",
    "RootNode",
    "decode_node",
    "encode_node",
    "key_hash",
    "node_hash",
]

_BITMAP_SIZE = FANOUT // BITS_PER_BYTE
_VARINT_PAYLOAD_BITS = 7
_VARINT_PAYLOAD_MASK = (1 << _VARINT_PAYLOAD_BITS) - 1
_VARINT_CONTINUATION = 1 << _VARINT_PAYLOAD_BITS
_HIGH_BIT = 1 << (BITS_PER_BYTE - 1)


class NodeType(IntEnum):
    LEAF = 1
    BRANCH = 2
    ROOT = 3


class NodeFormatError(ValueError):
    """Raised when node bytes are malformed, non-canonical or inconsistent."""


def key_hash(key: str) -> bytes:
    """Placement hash of an element key inside a partition: ``sha256(key)``."""
    return hashlib.sha256(key.encode("utf-8")).digest()


@dataclass(frozen=True, slots=True)
class LeafNode:
    """Leaf at ``depth`` holding ``(element_key, object_hash)`` entries sorted by key hash."""

    depth: int
    entries: tuple[tuple[str, bytes], ...]

    @property
    def count(self) -> int:
        return len(self.entries)


@dataclass(frozen=True, slots=True)
class BranchNode:
    """Branch at ``depth``: ``(byte, child_hash)`` pairs in byte order and the subtree count."""

    depth: int
    children: tuple[tuple[int, bytes], ...]
    count: int


@dataclass(frozen=True, slots=True)
class PartitionRef:
    """Root hash and entry count of one non-empty partition."""

    root: bytes
    count: int


@dataclass(frozen=True, slots=True)
class RootNode:
    """Revision tree root: non-empty partitions sorted by the UTF-8 bytes of their id."""

    partitions: tuple[tuple[str, PartitionRef], ...]

    @property
    def count(self) -> int:
        return sum(ref.count for _, ref in self.partitions)


Node = LeafNode | BranchNode | RootNode


def encode_node(node: Node) -> bytes:
    """Encode a node to its canonical bytes, validating its invariants."""
    out = bytearray()
    if isinstance(node, LeafNode):
        _check_depth(node.depth)
        if not node.entries:
            raise NodeFormatError("a leaf holds at least one entry")
        previous = b""
        for key, value in node.entries:
            current = key_hash(key)
            if current <= previous:
                raise NodeFormatError("leaf entries must be strictly sorted by key hash")
            previous = current
            _write_bytes(out, _utf8(key))
            _write_hash(out, value)
        return bytes(_header(NodeType.LEAF, node.depth, len(node.entries), node.count) + out)
    if isinstance(node, BranchNode):
        _check_depth(node.depth)
        if not node.children:
            raise NodeFormatError("a branch holds at least one child")
        bitmap = bytearray(_BITMAP_SIZE)
        last = -1
        for index, child in node.children:
            if not last < index < FANOUT:
                raise NodeFormatError("branch children must be strictly sorted bytes")
            last = index
            bitmap[index // BITS_PER_BYTE] |= _HIGH_BIT >> (index % BITS_PER_BYTE)
            _write_hash(out, child)
        if node.count < len(node.children):
            raise NodeFormatError("branch subtree count is smaller than its child count")
        header = _header(NodeType.BRANCH, node.depth, len(node.children), node.count)
        return bytes(header + bitmap + out)
    previous_id = b""
    for index, (partition, ref) in enumerate(node.partitions):
        encoded = _utf8(partition)
        if not encoded or (index and encoded <= previous_id):
            raise NodeFormatError("partition ids must be non-empty and strictly sorted")
        if ref.count < 1:
            raise NodeFormatError("empty partitions are omitted from the root")
        previous_id = encoded
        _write_bytes(out, encoded)
        _write_hash(out, ref.root)
        _write_varint(out, ref.count)
    return bytes(_header(NodeType.ROOT, 0, len(node.partitions), node.count) + out)


def decode_node(data: bytes) -> Node:
    """Decode canonical node bytes; raise :class:`NodeFormatError` on any deviation."""
    reader = _Reader(data)
    version = reader.byte()
    if version != NODE_FORMAT_VERSION:
        raise NodeFormatError(f"unsupported node format version {version}")
    try:
        node_type = NodeType(reader.byte())
    except ValueError as exc:
        raise NodeFormatError("unknown node type") from exc
    depth = reader.byte()
    entry_count = reader.varint()
    subtree_count = reader.varint()
    node: Node
    if node_type is NodeType.LEAF:
        entries = []
        for _ in range(entry_count):
            key = _from_utf8(reader.take(reader.varint()))
            entries.append((key, reader.take(HASH_SIZE)))
        node = LeafNode(depth, tuple(entries))
        if subtree_count != entry_count:
            raise NodeFormatError("leaf subtree count must equal its entry count")
    elif node_type is NodeType.BRANCH:
        bitmap = reader.take(_BITMAP_SIZE)
        indexes = [
            i
            for i in range(FANOUT)
            if bitmap[i // BITS_PER_BYTE] & (_HIGH_BIT >> (i % BITS_PER_BYTE))
        ]
        if len(indexes) != entry_count:
            raise NodeFormatError("branch bitmap does not match its entry count")
        node = BranchNode(depth, tuple((i, reader.take(HASH_SIZE)) for i in indexes), subtree_count)
    else:
        partitions = []
        for _ in range(entry_count):
            partition = _from_utf8(reader.take(reader.varint()))
            root = reader.take(HASH_SIZE)
            partitions.append((partition, PartitionRef(root, reader.varint())))
        node = RootNode(tuple(partitions))
        if depth != 0 or node.count != subtree_count:
            raise NodeFormatError("root header is inconsistent with its partitions")
    reader.finish()
    if encode_node(node) != data:
        raise NodeFormatError("node bytes are not in canonical form")
    return node


def _header(node_type: NodeType, depth: int, entries: int, subtree: int) -> bytearray:
    out = bytearray((NODE_FORMAT_VERSION, node_type, depth))
    _write_varint(out, entries)
    _write_varint(out, subtree)
    return out


def _check_depth(depth: int) -> None:
    if not 0 <= depth < HASH_SIZE:
        raise NodeFormatError(f"depth {depth} out of range")


def _utf8(text: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise NodeFormatError("keys must be valid Unicode text") from exc


def _from_utf8(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NodeFormatError("keys must be valid UTF-8") from exc


def _write_varint(out: bytearray, value: int) -> None:
    if value < 0:
        raise NodeFormatError("varints are unsigned")
    while True:
        low = value & _VARINT_PAYLOAD_MASK
        value >>= _VARINT_PAYLOAD_BITS
        if value:
            out.append(low | _VARINT_CONTINUATION)
        else:
            out.append(low)
            return


def _write_bytes(out: bytearray, data: bytes) -> None:
    _write_varint(out, len(data))
    out += data


def _write_hash(out: bytearray, value: bytes) -> None:
    if len(value) != HASH_SIZE:
        raise NodeFormatError(f"hashes are {HASH_SIZE} bytes")
    out += value


class _Reader:
    __slots__ = ("_data", "_pos")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def byte(self) -> int:
        return self.take(1)[0]

    def take(self, size: int) -> bytes:
        end = self._pos + size
        if end > len(self._data):
            raise NodeFormatError("truncated node")
        chunk = self._data[self._pos : end]
        self._pos = end
        return bytes(chunk)

    def varint(self) -> int:
        value = 0
        shift = 0
        while True:
            byte = self.byte()
            value |= (byte & _VARINT_PAYLOAD_MASK) << shift
            if not byte & _VARINT_CONTINUATION:
                return value
            shift += _VARINT_PAYLOAD_BITS

    def finish(self) -> None:
        if self._pos != len(self._data):
            raise NodeFormatError("trailing bytes after node")
