from __future__ import annotations

import hashlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lorebase_core.tree import (
    BranchNode,
    LeafNode,
    NodeFormatError,
    PartitionRef,
    RootNode,
    decode_node,
    encode_node,
    key_hash,
)
from lorebase_core.tree.node import node_hash

from .support import value


def _leaf(keys: list[str], depth: int = 0) -> LeafNode:
    ordered = sorted(set(keys), key=key_hash)
    return LeafNode(depth, tuple((k, value(k)) for k in ordered))


def test_leaf_round_trip() -> None:
    node = _leaf(["a", "b", "été", "x" * 300], depth=3)
    assert decode_node(encode_node(node)) == node


def test_branch_round_trip() -> None:
    node = BranchNode(1, ((0, value(0)), (7, value(7)), (255, value(255))), 1000)
    data = encode_node(node)
    assert decode_node(data) == node


def test_root_round_trip() -> None:
    node = RootNode(
        (
            ("k:knd_a", PartitionRef(value("a"), 3)),
            ("r:rel_b", PartitionRef(value("b"), 200)),
            ("schema", PartitionRef(value("s"), 1)),
        )
    )
    decoded = decode_node(encode_node(node))
    assert decoded == node
    assert isinstance(decoded, RootNode)
    assert decoded.count == 204


def test_empty_root_round_trip() -> None:
    assert decode_node(encode_node(RootNode(()))) == RootNode(())


def test_node_hash_rule() -> None:
    data = encode_node(_leaf(["a"]))
    assert node_hash(data) == hashlib.sha256(b"lb1\x00node\x00" + data).digest()


def test_header_layout() -> None:
    data = encode_node(BranchNode(2, ((5, value(5)),), 600))
    # version, type, depth, entry count, subtree count (600 as LEB128)
    assert data[:6] == bytes((1, 2, 2, 1, 0xD8, 0x04))
    assert len(data) == 6 + 32 + 32


@given(st.sets(st.text(max_size=12), min_size=1, max_size=40), st.integers(0, 31))
def test_leaf_round_trip_property(keys: set[str], depth: int) -> None:
    node = _leaf(sorted(keys), depth)
    assert decode_node(encode_node(node)) == node


@given(st.sets(st.integers(0, 255), min_size=1), st.integers(0, 31))
def test_branch_round_trip_property(indexes: set[int], depth: int) -> None:
    node = BranchNode(depth, tuple((i, value(i)) for i in sorted(indexes)), len(indexes) + 10)
    assert decode_node(encode_node(node)) == node


@pytest.mark.parametrize(
    "node",
    [
        LeafNode(0, ()),
        LeafNode(0, tuple(reversed(_leaf(["a", "b"]).entries))),
        LeafNode(0, (("a", value(1)), ("a", value(2)))),
        LeafNode(32, (("a", value(1)),)),
        LeafNode(0, (("a", b"short"),)),
        BranchNode(0, (), 0),
        BranchNode(0, ((3, value(3)), (2, value(2))), 10),
        BranchNode(0, ((256, value(3)),), 10),
        BranchNode(0, ((1, value(1)), (2, value(2))), 1),
        RootNode((("b", PartitionRef(value(1), 1)), ("a", PartitionRef(value(2), 1)))),
        RootNode((("a", PartitionRef(value(1), 0)),)),
        RootNode((("", PartitionRef(value(1), 1)),)),
    ],
)
def test_invalid_nodes_rejected(node: LeafNode | BranchNode | RootNode) -> None:
    with pytest.raises(NodeFormatError):
        encode_node(node)


def test_non_canonical_bytes_rejected() -> None:
    data = encode_node(_leaf(["a", "b"]))
    with pytest.raises(NodeFormatError, match="truncated"):
        decode_node(data[:-1])
    with pytest.raises(NodeFormatError, match="trailing"):
        decode_node(data + b"\x00")
    with pytest.raises(NodeFormatError, match="version"):
        decode_node(b"\x02" + data[1:])
    with pytest.raises(NodeFormatError, match="type"):
        decode_node(data[:1] + b"\x09" + data[2:])
    # Entry count 2 written as a two-byte varint is not canonical.
    padded = data[:3] + b"\x82\x00" + data[4:]
    with pytest.raises(NodeFormatError, match="canonical"):
        decode_node(padded)


def test_leaf_count_mismatch_rejected() -> None:
    data = bytearray(encode_node(_leaf(["a", "b"])))
    data[4] = 3  # subtree count differs from the entry count
    with pytest.raises(NodeFormatError):
        decode_node(bytes(data))
