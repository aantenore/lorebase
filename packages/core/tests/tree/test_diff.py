from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from lorebase_core.tree import (
    BranchNode,
    InMemoryNodeStore,
    Tree,
    TreeChange,
    TreeParams,
    key_hash,
    read_node,
    tree_apply,
    tree_diff,
    tree_entries,
)
from lorebase_core.tree.diff import DiffEntry

from .support import CountingStore, value

P = "k:knd_part"

keys_strategy = st.text(alphabet="abcdefgh0123456789", min_size=1, max_size=5)
entry_sets = st.dictionaries(
    st.tuples(st.sampled_from(["schema", "k:a", "k:b", "r:c"]), keys_strategy),
    st.integers(0, 3).map(value),
    max_size=80,
)
leaf_sizes = st.sampled_from([1, 2, 4, 512])


def _contents(store: InMemoryNodeStore, root: bytes) -> dict[tuple[str, str], bytes]:
    out: dict[tuple[str, str], bytes] = {}
    for partition in Tree.load(store, root).partitions:
        for key, h in tree_entries(store, root, partition):
            out[(partition, key)] = h
    return out


def _two_trees(
    a: dict[tuple[str, str], bytes], b: dict[tuple[str, str], bytes], params: TreeParams
) -> tuple[InMemoryNodeStore, bytes, bytes]:
    store = InMemoryNodeStore()
    root_a = tree_apply(store, None, [TreeChange(p, k, v) for (p, k), v in a.items()], params)
    changes = [TreeChange(p, k, b.get((p, k))) for (p, k) in a.keys() | b.keys()]
    root_b = tree_apply(store, root_a, changes, params)
    return store, root_a, root_b


@settings(max_examples=80, deadline=None)
@given(entry_sets, entry_sets, leaf_sizes)
def test_applying_diff_to_a_yields_b(
    a: dict[tuple[str, str], bytes], b: dict[tuple[str, str], bytes], leaf_size: int
) -> None:
    params = TreeParams(leaf_size=leaf_size)
    store, root_a, root_b = _two_trees(a, b, params)
    diff = list(tree_diff(store, root_a, root_b))
    patched = tree_apply(
        store, root_a, [TreeChange(d.partition, d.key, d.hash_b) for d in diff], params
    )
    assert patched == root_b


@settings(max_examples=80, deadline=None)
@given(entry_sets, entry_sets, leaf_sizes)
def test_diff_equals_the_net_changes(
    a: dict[tuple[str, str], bytes], b: dict[tuple[str, str], bytes], leaf_size: int
) -> None:
    store, root_a, root_b = _two_trees(a, b, TreeParams(leaf_size=leaf_size))
    expected = {
        (p, k): (a.get((p, k)), b.get((p, k)))
        for (p, k) in a.keys() | b.keys()
        if a.get((p, k)) != b.get((p, k))
    }
    diff = list(tree_diff(store, root_a, root_b))
    assert {(d.partition, d.key): (d.hash_a, d.hash_b) for d in diff} == expected
    assert len(diff) == len(expected)
    reverse = {(d.partition, d.key): (d.hash_a, d.hash_b) for d in tree_diff(store, root_b, root_a)}
    assert reverse == {pk: (hb, ha) for pk, (ha, hb) in expected.items()}


@settings(max_examples=40, deadline=None)
@given(entry_sets, leaf_sizes)
def test_diff_against_empty_tree_lists_everything(
    a: dict[tuple[str, str], bytes], leaf_size: int
) -> None:
    store, root_a, _ = _two_trees(a, a, TreeParams(leaf_size=leaf_size))
    added = {(d.partition, d.key): d.hash_b for d in tree_diff(store, None, root_a)}
    assert added == _contents(store, root_a)


def test_diff_with_itself_is_empty_and_reads_nothing() -> None:
    store = CountingStore()
    root = tree_apply(store, None, [(P, f"k{i}", value(i)) for i in range(2000)])
    store.reads.clear()
    assert list(tree_diff(store, root, root)) == []
    assert store.total_reads == 0


def test_equal_partitions_are_skipped() -> None:
    store = CountingStore()
    params = TreeParams(leaf_size=8)
    big = [("k:big", f"k{i}", value(i)) for i in range(5000)]
    root_a = tree_apply(store, None, [*big, ("k:small", "x", value("x"))], params)
    root_b = tree_apply(store, root_a, [("k:small", "x", value("y"))], params)
    store.reads.clear()
    assert list(tree_diff(store, root_a, root_b)) == [
        DiffEntry("k:small", "x", value("x"), value("y"))
    ]
    big_root = Tree.load(store, root_a).partitions["k:big"].root
    assert store.reads[big_root] == 0


def test_single_change_reads_at_most_depth_plus_one_per_side() -> None:
    store = CountingStore()
    total = 100_000
    root_a = tree_apply(store, None, ((P, f"R-{i}", value(i)) for i in range(total)))
    changed = "R-4242"
    root_b = tree_apply(store, root_a, [(P, changed, value("new"))])

    # Depth of the changed key: nodes on its path below the root node.
    depth = 0
    current = Tree.load(store, root_a).partitions[P].root
    while True:
        depth += 1
        node = read_node(store, current)
        if not isinstance(node, BranchNode):
            break
        current = dict(node.children)[key_hash(changed)[node.depth]]

    store.reads.clear()
    diff = list(tree_diff(store, root_a, root_b))
    assert diff == [DiffEntry(P, changed, value(4242), value("new"))]
    assert store.total_reads <= 2 * (depth + 1)
