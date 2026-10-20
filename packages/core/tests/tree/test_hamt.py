from __future__ import annotations

import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lorebase_core.tree import (
    BranchNode,
    InMemoryNodeStore,
    LeafNode,
    MissingNodeError,
    StagingNodeStore,
    Tree,
    TreeChange,
    TreeParams,
    empty_tree,
    kind_partition,
    link_key,
    read_node,
    relation_partition,
    tree_apply,
    tree_count,
    tree_entries,
    tree_get,
)

from .support import keys_with_prefix, value

P = "k:knd_supplier"


def _partition_root(store: InMemoryNodeStore, root: bytes, partition: str = P) -> bytes:
    return Tree.load(store, root).partitions[partition].root


def _build(
    entries: dict[tuple[str, str], bytes], params: TreeParams
) -> tuple[InMemoryNodeStore, bytes]:
    store = InMemoryNodeStore()
    changes = [TreeChange(p, k, v) for (p, k), v in entries.items()]
    return store, tree_apply(store, None, changes, params)


def test_empty_tree() -> None:
    store = InMemoryNodeStore()
    root = empty_tree(store)
    assert tree_apply(store, None, []) == root
    assert Tree.load(store, root).partitions == {}
    assert tree_get(store, root, P, "x") is None
    assert tree_count(store, root, P) == 0
    assert list(tree_entries(store, root, P)) == []


def test_get_count_entries() -> None:
    store = InMemoryNodeStore()
    root = tree_apply(
        store,
        None,
        [(P, "S-1", value(1)), (P, "S-2", value(2)), ("schema", "knd_supplier", value("k"))],
    )
    assert tree_get(store, root, P, "S-1") == value(1)
    assert tree_get(store, root, P, "S-3") is None
    assert tree_get(store, root, "schema", "knd_supplier") == value("k")
    assert tree_count(store, root, P) == 2
    assert tree_count(store, root, "schema") == 1
    assert Tree.load(store, root).count == 3
    assert dict(tree_entries(store, root, P)) == {"S-1": value(1), "S-2": value(2)}


def test_update_and_noop_changes() -> None:
    store = InMemoryNodeStore()
    root = tree_apply(store, None, [(P, "a", value(1))])
    assert tree_apply(store, root, []) == root
    assert tree_apply(store, root, [(P, "a", value(1))]) == root
    assert tree_apply(store, root, [(P, "missing", None)]) == root
    updated = tree_apply(store, root, [(P, "a", value(2))])
    assert updated != root
    assert tree_get(store, updated, P, "a") == value(2)
    assert tree_get(store, root, P, "a") == value(1)


def test_removing_all_entries_yields_empty_tree_hash() -> None:
    store = InMemoryNodeStore()
    keys = [f"S-{i}" for i in range(50)]
    params = TreeParams(leaf_size=4)
    root = tree_apply(store, None, [(P, k, value(k)) for k in keys], params)
    emptied = tree_apply(store, root, [(P, k, None) for k in keys], params)
    assert emptied == empty_tree(store)


def test_partition_helpers() -> None:
    assert kind_partition("knd_1") == "k:knd_1"
    assert relation_partition("rel_1") == "r:rel_1"
    assert link_key("P-1", "S-1") == "P-1\x1fS-1"


def test_invalid_changes_rejected() -> None:
    store = InMemoryNodeStore()
    with pytest.raises(ValueError, match="duplicate"):
        tree_apply(store, None, [(P, "a", value(1)), (P, "a", None)])
    with pytest.raises(ValueError, match="32 bytes"):
        tree_apply(store, None, [(P, "a", b"short")])
    with pytest.raises(ValueError, match="partition"):
        tree_apply(store, None, [("", "a", value(1))])


def test_missing_node_raises() -> None:
    store = InMemoryNodeStore()
    root = tree_apply(store, None, [(P, "a", value(1))])
    with pytest.raises(MissingNodeError):
        tree_get(InMemoryNodeStore(), root, P, "a")


def test_staging_store_collects_only_new_nodes() -> None:
    base = InMemoryNodeStore()
    params = TreeParams(leaf_size=4)
    root = tree_apply(base, None, [(P, f"k{i}", value(i)) for i in range(40)], params)
    size = len(base)
    staging = StagingNodeStore(base)
    new_root = tree_apply(staging, root, [(P, "k0", value("changed"))], params)
    assert len(base) == size
    assert new_root in staging.written
    assert all(not base.has(h) for h in staging.written)
    # New root, partition branch and one leaf.
    assert len(staging.written) == 3
    assert tree_get(staging, new_root, P, "k0") == value("changed")


class TestSmallLeafBoundaries:
    """Splits and merges at the leaf threshold, with ``L = 4`` and crafted keys."""

    params = TreeParams(leaf_size=4)

    def test_leaf_up_to_threshold(self) -> None:
        store = InMemoryNodeStore()
        keys = keys_with_prefix(b"", 4)
        root = tree_apply(store, None, [(P, k, value(k)) for k in keys], self.params)
        node = read_node(store, _partition_root(store, root))
        assert isinstance(node, LeafNode)
        assert node.depth == 0 and node.count == 4

    def test_split_when_threshold_exceeded(self) -> None:
        store = InMemoryNodeStore()
        keys = keys_with_prefix(b"", 5)
        root = tree_apply(store, None, [(P, k, value(k)) for k in keys], self.params)
        node = read_node(store, _partition_root(store, root))
        assert isinstance(node, BranchNode)
        assert node.depth == 0 and node.count == 5
        for _, child in node.children:
            child_node = read_node(store, child)
            assert isinstance(child_node, LeafNode) and child_node.depth == 1

    def test_merge_back_when_below_threshold(self) -> None:
        store = InMemoryNodeStore()
        keys = keys_with_prefix(b"", 5)
        four = tree_apply(store, None, [(P, k, value(k)) for k in keys[:4]], self.params)
        five = tree_apply(store, four, [(P, keys[4], value(keys[4]))], self.params)
        assert five != four
        back = tree_apply(store, five, [(P, keys[4], None)], self.params)
        assert back == four
        node = read_node(store, _partition_root(store, back))
        assert isinstance(node, LeafNode)

    def test_shared_prefix_nests_branches(self) -> None:
        store = InMemoryNodeStore()
        prefix = bytes([0xAB])
        keys = keys_with_prefix(prefix, 5)
        root = tree_apply(store, None, [(P, k, value(k)) for k in keys], self.params)
        top = read_node(store, _partition_root(store, root))
        assert isinstance(top, BranchNode)
        assert [index for index, _ in top.children] == [0xAB]
        inner = read_node(store, top.children[0][1])
        assert isinstance(inner, BranchNode) and inner.depth == 1 and inner.count == 5
        for k in keys:
            assert tree_get(store, root, P, k) == value(k)
        collapsed = tree_apply(store, root, [(P, keys[0], None)], self.params)
        leaf = read_node(store, _partition_root(store, collapsed))
        assert isinstance(leaf, LeafNode) and leaf.depth == 0 and leaf.count == 4

    def test_deep_branch_collapse_keeps_unchanged_children(self) -> None:
        store = InMemoryNodeStore()
        group_a = keys_with_prefix(bytes([0x01]), 5, stem="a")
        group_b = keys_with_prefix(bytes([0x02]), 3, stem="b")
        everything = group_a + group_b
        root = tree_apply(store, None, [(P, k, value(k)) for k in everything], self.params)
        removed = tree_apply(store, root, [(P, k, None) for k in group_a[:2]], self.params)
        expected_store, expected = _build({(P, k): value(k) for k in everything[2:]}, self.params)
        assert removed == expected
        assert dict(tree_entries(store, removed, P)) == dict(
            tree_entries(expected_store, expected, P)
        )


keys_strategy = st.text(alphabet="abcdefghij0123456789-", min_size=1, max_size=6)
entry_sets = st.dictionaries(
    st.tuples(st.sampled_from(["schema", "k:a", "k:b", "r:c"]), keys_strategy),
    st.integers(0, 3).map(value),
    max_size=80,
)
leaf_sizes = st.sampled_from([1, 2, 4, 16, 512])


@settings(max_examples=60, deadline=None)
@given(entry_sets, entry_sets, leaf_sizes, st.randoms(use_true_random=False))
def test_root_depends_only_on_final_set(
    final: dict[tuple[str, str], bytes],
    noise: dict[tuple[str, str], bytes],
    leaf_size: int,
    rnd: random.Random,
) -> None:
    params = TreeParams(leaf_size=leaf_size)
    _, expected = _build(final, params)

    # Reach the same final set through random batches: insert noise and final
    # entries in random order, then delete noise and fix values.
    store = InMemoryNodeStore()
    root = tree_apply(store, None, [], params)
    steps: list[TreeChange] = [TreeChange(p, k, v) for (p, k), v in noise.items()]
    steps += [TreeChange(p, k, v) for (p, k), v in final.items()]
    rnd.shuffle(steps)
    finishing = [TreeChange(p, k, final.get((p, k))) for (p, k) in noise.keys() | final.keys()]
    rnd.shuffle(finishing)
    for sequence in (steps, finishing):
        while sequence:
            size = rnd.randint(1, 10)
            batch, sequence = sequence[:size], sequence[size:]
            unique = {(c.partition, c.key): c for c in batch}
            root = tree_apply(store, root, list(unique.values()), params)
    # The two passes may leave a stale value when a batch dropped duplicates;
    # a final pass makes the set exact.
    root = tree_apply(
        store,
        root,
        [TreeChange(p, k, final.get((p, k))) for (p, k) in noise.keys() | final.keys()],
        params,
    )
    assert root == expected
    for (p, k), v in final.items():
        assert tree_get(store, root, p, k) == v


@settings(max_examples=60, deadline=None)
@given(entry_sets, entry_sets, leaf_sizes)
def test_apply_then_remove_restores_previous_root(
    base: dict[tuple[str, str], bytes],
    extra: dict[tuple[str, str], bytes],
    leaf_size: int,
) -> None:
    params = TreeParams(leaf_size=leaf_size)
    store, root = _build(base, params)
    added = {pk: v for pk, v in extra.items() if pk not in base}
    grown = tree_apply(store, root, [TreeChange(p, k, v) for (p, k), v in added.items()], params)
    restored = tree_apply(store, grown, [TreeChange(p, k, None) for (p, k) in added], params)
    assert restored == root


@settings(max_examples=40, deadline=None)
@given(entry_sets, leaf_sizes)
def test_counts_and_entries_match_content(
    entries: dict[tuple[str, str], bytes], leaf_size: int
) -> None:
    store, root = _build(entries, TreeParams(leaf_size=leaf_size))
    for partition in {p for p, _ in entries}:
        expected = {k: v for (p, k), v in entries.items() if p == partition}
        assert tree_count(store, root, partition) == len(expected)
        assert dict(tree_entries(store, root, partition)) == expected


@pytest.mark.slow
def test_one_million_entries() -> None:
    store = InMemoryNodeStore()
    total = 1_000_000
    root = tree_apply(store, None, ((P, f"R-{i}", value(i)) for i in range(total)))
    assert tree_count(store, root, P) == total
    rnd = random.Random(7)
    for i in rnd.sample(range(total), 200):
        assert tree_get(store, root, P, f"R-{i}") == value(i)
    assert tree_get(store, root, P, "R-absent") is None
