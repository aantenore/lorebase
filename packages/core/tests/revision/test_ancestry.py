from __future__ import annotations

import hashlib
from collections.abc import Sequence

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lorebase_core.revision import (
    InMemoryRevisionGraph,
    MergeBaseError,
    MergeStatus,
    UnknownRevisionError,
    is_ancestor,
    lowest_common_ancestors,
    merge_base,
)


def rid(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


class History:
    """Builds a graph from labels: ``add("C", "A", "B")`` creates C with parents A and B."""

    def __init__(self) -> None:
        self.graph = InMemoryRevisionGraph()

    def add(self, label: str, *parents: str) -> bytes:
        self.graph.add(rid(label), [rid(p) for p in parents])
        return rid(label)


class RecordingBuilder:
    """Virtual base builder that records its calls and returns deterministic ids."""

    def __init__(self) -> None:
        self.calls: list[tuple[bytes | None, bytes, bytes]] = []

    def __call__(self, base: bytes | None, ours: bytes, theirs: bytes) -> bytes:
        self.calls.append((base, ours, theirs))
        return rid(f"virtual-{len(self.calls)}")


def test_generations() -> None:
    h = History()
    h.add("A")
    h.add("B", "A")
    h.add("C", "A")
    h.add("D", "B", "C")
    h.add("E", "D")
    assert [h.graph.generation(rid(x)) for x in "ABCDE"] == [1, 2, 2, 3, 4]


def test_unknown_revisions_rejected() -> None:
    h = History()
    h.add("A")
    with pytest.raises(UnknownRevisionError):
        h.add("B", "missing")
    with pytest.raises(UnknownRevisionError):
        merge_base(h.graph, rid("A"), rid("missing"))
    with pytest.raises(ValueError, match="different parents"):
        h.graph.add(rid("A"), [rid("A")])


def test_same_revision_is_up_to_date() -> None:
    h = History()
    a = h.add("A")
    result = merge_base(h.graph, a, a)
    assert result.status is MergeStatus.UP_TO_DATE
    assert result.base == a


def test_linear_history() -> None:
    h = History()
    a = h.add("A")
    h.add("B", "A")
    c = h.add("C", "B")
    behind = merge_base(h.graph, c, a)
    assert behind.status is MergeStatus.UP_TO_DATE
    assert behind.base == a
    ahead = merge_base(h.graph, a, c)
    assert ahead.status is MergeStatus.FAST_FORWARD
    assert ahead.base == a
    assert ahead.common_ancestors == (a,)


def test_divergent_history() -> None:
    h = History()
    a = h.add("A")
    h.add("B1", "A")
    b = h.add("B2", "B1")
    c = h.add("C1", "A")
    result = merge_base(h.graph, b, c)
    assert result.status is MergeStatus.MERGE
    assert result.base == a
    assert result.common_ancestors == (a,)
    assert not result.virtual


def test_after_a_merge_the_base_moves() -> None:
    h = History()
    h.add("A")
    h.add("B1", "A")
    c1 = h.add("C1", "A")
    h.add("B2", "B1", "C1")  # theirs merged into ours
    b3 = h.add("B3", "B2")
    c2 = h.add("C2", "C1")
    result = merge_base(h.graph, b3, c2)
    assert result.status is MergeStatus.MERGE
    assert result.base == c1


def test_unrelated_histories() -> None:
    h = History()
    a = h.add("A")
    b = h.add("B")
    result = merge_base(h.graph, a, b)
    assert result.status is MergeStatus.UNRELATED
    assert result.base is None
    assert result.common_ancestors == ()


def _criss_cross(h: History) -> tuple[bytes, bytes]:
    h.add("A")
    h.add("B1", "A")
    h.add("C1", "A")
    h.add("B2", "B1", "C1")
    h.add("C2", "C1", "B1")
    return h.add("B3", "B2"), h.add("C3", "C2")


def test_criss_cross_builds_virtual_base() -> None:
    h = History()
    ours, theirs = _criss_cross(h)
    builder = RecordingBuilder()
    result = merge_base(h.graph, ours, theirs, builder)
    lcas = tuple(sorted([rid("B1"), rid("C1")]))
    assert result.common_ancestors == lcas
    assert builder.calls == [(rid("A"), lcas[0], lcas[1])]
    assert result.status is MergeStatus.MERGE
    assert result.base == rid("virtual-1")
    assert result.virtual
    assert [(v.id, v.parents) for v in result.virtual_revisions] == [(rid("virtual-1"), lcas)]
    # The caller's graph is not modified.
    assert rid("virtual-1") not in h.graph


def test_criss_cross_without_builder_fails() -> None:
    h = History()
    ours, theirs = _criss_cross(h)
    with pytest.raises(MergeBaseError):
        merge_base(h.graph, ours, theirs)


def test_three_common_ancestors_fold_pairwise() -> None:
    h = History()
    h.add("A")
    for label in "XYZ":
        h.add(label, "A")
    h.add("O1", "X", "Y")
    ours = h.add("O2", "O1", "Z")
    h.add("T1", "Z", "Y")
    theirs = h.add("T2", "T1", "X")
    builder = RecordingBuilder()
    result = merge_base(h.graph, ours, theirs, builder)
    x, y, z = sorted(rid(n) for n in "XYZ")
    assert result.common_ancestors == (x, y, z)
    assert builder.calls == [(rid("A"), x, y), (rid("A"), rid("virtual-1"), z)]
    assert result.base == rid("virtual-2")


def test_recursive_virtual_base() -> None:
    h = History()
    _criss_cross(h)  # B1, C1 are both lowest common ancestors of B2 and C2
    ours = h.add("P", "B2", "C2")
    theirs = h.add("Q", "C2", "B2")
    builder = RecordingBuilder()
    result = merge_base(h.graph, ours, theirs, builder)
    outer = tuple(sorted([rid("B2"), rid("C2")]))
    inner = tuple(sorted([rid("B1"), rid("C1")]))
    assert result.common_ancestors == outer
    assert builder.calls == [
        (rid("A"), inner[0], inner[1]),
        (rid("virtual-1"), outer[0], outer[1]),
    ]
    assert result.base == rid("virtual-2")
    assert len(result.virtual_revisions) == 2


def test_virtual_pair_without_common_ancestor() -> None:
    h = History()
    h.add("R1")
    h.add("R2")
    ours = h.add("O", "R1", "R2")
    theirs = h.add("T", "R2", "R1")
    builder = RecordingBuilder()
    result = merge_base(h.graph, ours, theirs, builder)
    r1, r2 = sorted([rid("R1"), rid("R2")])
    assert builder.calls == [(None, r1, r2)]
    assert result.base == rid("virtual-1")


def test_is_ancestor() -> None:
    h = History()
    ours, theirs = _criss_cross(h)
    assert is_ancestor(h.graph, rid("A"), ours)
    assert is_ancestor(h.graph, rid("C1"), ours)
    assert is_ancestor(h.graph, ours, ours)
    assert not is_ancestor(h.graph, ours, theirs)
    assert not is_ancestor(h.graph, rid("B3"), rid("A"))


# Property tests against a brute-force reference on random DAGs.


@st.composite
def dags(draw: st.DrawFn) -> list[list[int]]:
    size = draw(st.integers(1, 25))
    parents: list[list[int]] = []
    for i in range(size):
        if i == 0:
            parents.append([])
            continue
        count = draw(st.integers(0 if i < 3 else 1, min(3, i)))
        parents.append(
            draw(st.lists(st.integers(0, i - 1), min_size=count, max_size=count, unique=True))
        )
    return parents


def _graph(parents: Sequence[Sequence[int]]) -> InMemoryRevisionGraph:
    graph = InMemoryRevisionGraph()
    for i, ps in enumerate(parents):
        graph.add(rid(str(i)), [rid(str(p)) for p in ps])
    return graph


def _ancestors(parents: Sequence[Sequence[int]], node: int) -> set[int]:
    seen = {node}
    stack = [node]
    while stack:
        for p in parents[stack.pop()]:
            if p not in seen:
                seen.add(p)
                stack.append(p)
    return seen


@settings(max_examples=200, deadline=None)
@given(dags(), st.data())
def test_lowest_common_ancestors_match_reference(
    parents: list[list[int]], data: st.DataObject
) -> None:
    graph = _graph(parents)
    a = data.draw(st.integers(0, len(parents) - 1))
    b = data.draw(st.integers(0, len(parents) - 1))
    common = _ancestors(parents, a) & _ancestors(parents, b)
    lowest = {c for c in common if not any(c != d and c in _ancestors(parents, d) for d in common)}
    found = lowest_common_ancestors(graph, rid(str(a)), rid(str(b)))
    assert set(found) == {rid(str(c)) for c in lowest}
    assert len(found) == len(set(found))
    assert is_ancestor(graph, rid(str(a)), rid(str(b))) == (a in _ancestors(parents, b))
