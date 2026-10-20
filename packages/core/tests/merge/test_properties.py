"""Merge laws as property tests."""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import given, settings
from hypothesis import strategies as st

from lorebase_core.merge import Conflict, MergeAttempt, MergedTree, MergePolicy, MergeResult
from lorebase_core.model import Record

from .support import Edit, Repo, base_schema, supplier

_SETTINGS = settings(max_examples=40, deadline=None)

#: Per-key operation: keep, rename to one of a few names, or delete.
_OPERATION = st.one_of(st.none(), st.sampled_from(["alpha", "beta", "gamma", "delete"]))


@dataclass(frozen=True)
class World:
    size: int
    ours: tuple[str | None, ...]
    theirs: tuple[str | None, ...]
    ours_added: int
    theirs_added: int


def _base(size: int) -> list[Record]:
    return [supplier(f"S-{i}") for i in range(size)]


def _edit(operations: tuple[str | None, ...], added: list[Record]) -> Edit:
    edit = Edit().upsert(*added)
    for i, operation in enumerate(operations):
        if operation == "delete":
            edit.remove(supplier(f"S-{i}"))
        elif operation is not None:
            edit.upsert(supplier(f"S-{i}", operation))
    return edit


@st.composite
def worlds(draw: st.DrawFn, *, disjoint: bool) -> World:
    size = draw(st.integers(min_value=1, max_value=8))
    ours = draw(st.lists(_OPERATION, min_size=size, max_size=size))
    theirs = draw(st.lists(_OPERATION, min_size=size, max_size=size))
    if disjoint:
        owner = draw(st.lists(st.booleans(), min_size=size, max_size=size))
        ours = [op if mine else None for op, mine in zip(ours, owner, strict=True)]
        theirs = [op if not mine else None for op, mine in zip(theirs, owner, strict=True)]
    return World(
        size,
        tuple(ours),
        tuple(theirs),
        draw(st.integers(min_value=0, max_value=3)),
        draw(st.integers(min_value=0, max_value=3)),
    )


def _build(world: World) -> tuple[Repo, bytes, bytes, bytes, Edit, Edit]:
    repo = Repo()
    base = repo.commit("base", Edit(schema=base_schema(), put=list(_base(world.size))))
    ours_edit = _edit(world.ours, [supplier(f"O-{i}") for i in range(world.ours_added)])
    theirs_edit = _edit(world.theirs, [supplier(f"T-{i}") for i in range(world.theirs_added)])
    ours = repo.commit("ours", ours_edit, base)
    theirs = repo.commit("theirs", theirs_edit, base)
    return repo, base, ours, theirs, ours_edit, theirs_edit


@_SETTINGS
@given(worlds(disjoint=False))
def test_merging_a_branch_with_itself_is_a_no_op(world: World) -> None:
    repo, _, ours, _, _, _ = _build(world)
    result = repo.merge(ours, ours)
    assert isinstance(result, MergedTree)
    assert result.result is MergeResult.UP_TO_DATE
    assert result.root == repo.roots[ours]
    assert result.changes == ()


@_SETTINGS
@given(worlds(disjoint=True))
def test_disjoint_changes_merge_to_both_applied(world: World) -> None:
    repo, base, ours, theirs, ours_edit, theirs_edit = _build(world)
    both = Edit(put=ours_edit.put + theirs_edit.put, delete=ours_edit.delete + theirs_edit.delete)
    expected = repo.write(repo.roots[base], both)
    forward = repo.merge(ours, theirs)
    backward = repo.merge(theirs, ours)
    assert isinstance(forward, MergedTree)
    assert isinstance(backward, MergedTree)
    assert forward.root == expected == backward.root


@_SETTINGS
@given(worlds(disjoint=False))
def test_conflict_set_is_symmetric_up_to_labels(world: World) -> None:
    repo, _, ours, theirs, _, _ = _build(world)
    forward = repo.merge(ours, theirs)
    backward = repo.merge(theirs, ours)
    assert type(forward) is type(backward)
    if isinstance(forward, MergeAttempt):
        assert isinstance(backward, MergeAttempt)
        assert _labels(forward.conflicts) == _labels(backward.conflicts, swap=True)
    else:
        assert isinstance(backward, MergedTree)
        assert forward.root == backward.root


def _labels(conflicts: tuple[Conflict, ...], *, swap: bool = False) -> set[tuple[object, ...]]:
    result = set()
    for c in conflicts:
        ours, theirs = (c.theirs, c.ours) if swap else (c.ours, c.theirs)
        result.add(
            (c.kind, c.partition, c.key, c.attribute, repr(c.base), repr(ours), repr(theirs))
        )
    return result


@_SETTINGS
@given(worlds(disjoint=False), st.booleans())
def test_fast_forward_detection(world: World, allowed: bool) -> None:
    repo, base, ours, theirs, _, theirs_edit = _build(world)
    descendant = repo.commit("descendant", theirs_edit, ours)
    policy = MergePolicy(fast_forward=allowed)
    forward = repo.merge(ours, descendant, policy=policy)
    assert isinstance(forward, MergedTree)
    assert forward.root == repo.roots[descendant]
    if allowed:
        assert forward.result is MergeResult.FAST_FORWARD
        assert forward.parents == ()
    else:
        assert forward.result is MergeResult.MERGED
        assert forward.parents == (ours, descendant)
    backward = repo.merge(descendant, ours)
    assert isinstance(backward, MergedTree)
    assert backward.result is MergeResult.UP_TO_DATE
    assert backward.root == repo.roots[descendant]
    assert base != theirs


@_SETTINGS
@given(worlds(disjoint=False))
def test_same_changes_on_both_sides_merge_to_them(world: World) -> None:
    repo, base, ours, _, ours_edit, _ = _build(world)
    twin = repo.commit("twin", ours_edit, base)
    result = repo.merge(ours, twin)
    assert isinstance(result, MergedTree)
    assert result.root == repo.roots[ours] == repo.roots[twin]
    assert result.changes == ()
