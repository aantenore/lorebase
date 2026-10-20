"""Revision ancestry: generation numbers, lowest common ancestors and merge base.

Design 6.4 and 6.8 step 0, AC-13.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from lorebase_core.revision.revision import Revision, next_generation, revision_id

__all__ = [
    "InMemoryRevisionGraph",
    "MergeBaseError",
    "MergeBaseResult",
    "MergeStatus",
    "RevisionGraph",
    "UnknownRevisionError",
    "VirtualBaseBuilder",
    "VirtualRevision",
    "is_ancestor",
    "lowest_common_ancestors",
    "merge_base",
]


class UnknownRevisionError(KeyError):
    """Raised when a Revision id is not in the graph."""


class MergeBaseError(ValueError):
    """Raised when a merge base cannot be determined."""


@runtime_checkable
class RevisionGraph(Protocol):
    """Read access to the Revision DAG."""

    def parents(self, revision_id: bytes) -> Sequence[bytes]: ...

    def generation(self, revision_id: bytes) -> int: ...


class InMemoryRevisionGraph:
    """Dictionary-backed :class:`RevisionGraph`."""

    def __init__(self) -> None:
        self._parents: dict[bytes, tuple[bytes, ...]] = {}
        self._generations: dict[bytes, int] = {}

    def add(self, revision: bytes, parents: Iterable[bytes] = ()) -> int:
        """Register a Revision whose parents are already known; return its generation."""
        parent_ids = tuple(parents)
        known = self._parents.get(revision)
        if known is not None:
            if known != parent_ids:
                raise ValueError("revision already registered with different parents")
            return self._generations[revision]
        generation = next_generation(self.generation(p) for p in parent_ids)
        self._parents[revision] = parent_ids
        self._generations[revision] = generation
        return generation

    def add_revision(self, revision: Revision) -> bytes:
        """Register a Revision object and return its id."""
        rev_id = revision_id(revision)
        self.add(rev_id, revision.parents)
        return rev_id

    def parents(self, revision_id: bytes) -> Sequence[bytes]:
        try:
            return self._parents[revision_id]
        except KeyError:
            raise UnknownRevisionError(revision_id.hex()) from None

    def generation(self, revision_id: bytes) -> int:
        try:
            return self._generations[revision_id]
        except KeyError:
            raise UnknownRevisionError(revision_id.hex()) from None

    def __contains__(self, revision_id: object) -> bool:
        return revision_id in self._parents


def is_ancestor(graph: RevisionGraph, ancestor: bytes, descendant: bytes) -> bool:
    """True when ``ancestor`` is ``descendant`` or reachable from it through parents."""
    if ancestor == descendant:
        graph.generation(ancestor)
        return True
    floor = graph.generation(ancestor)
    seen = {descendant}
    stack = [descendant]
    while stack:
        for parent in graph.parents(stack.pop()):
            if parent == ancestor:
                return True
            # Parents always have a lower generation, so nothing at or below
            # the ancestor's generation can lead to it.
            if parent not in seen and graph.generation(parent) > floor:
                seen.add(parent)
                stack.append(parent)
    return False


_OURS = 1
_THEIRS = 2
_STALE = 4
_BOTH = _OURS | _THEIRS


def lowest_common_ancestors(graph: RevisionGraph, a: bytes, b: bytes) -> tuple[bytes, ...]:
    """All lowest common ancestors of ``a`` and ``b``, ordered by (generation, id).

    Walks both histories at once in decreasing generation order. Parents always
    have a lower generation than their children, so when a Revision is visited
    every path from ``a`` or ``b`` to it has already been seen: a Revision
    reached from both sides is a common ancestor, and everything below it is
    marked stale. The walk stops as soon as no unvisited, non-stale Revision
    is reachable from both sides.
    """
    if a == b:
        graph.generation(a)
        return (a,)
    flags = {a: _OURS, b: _THEIRS}
    heap = [(-graph.generation(a), a), (-graph.generation(b), b)]
    heapq.heapify(heap)
    pending: dict[int, set[bytes]] = {_OURS: {a}, _THEIRS: {b}}
    visited: set[bytes] = set()
    found: list[bytes] = []
    while pending[_OURS] and pending[_THEIRS]:
        _, current = heapq.heappop(heap)
        if current in visited:
            continue
        visited.add(current)
        pending[_OURS].discard(current)
        pending[_THEIRS].discard(current)
        state = flags[current]
        if state & _BOTH == _BOTH and not state & _STALE:
            found.append(current)
            state |= _STALE
            flags[current] = state
        for parent in graph.parents(current):
            old = flags.get(parent, 0)
            new = old | state
            if new == old:
                continue
            flags[parent] = new
            heapq.heappush(heap, (-graph.generation(parent), parent))
            for side in (_OURS, _THEIRS):
                if new & side and not new & _STALE:
                    pending[side].add(parent)
                else:
                    pending[side].discard(parent)
    return tuple(sorted(found, key=lambda rev: (graph.generation(rev), rev)))


class MergeStatus(StrEnum):
    UP_TO_DATE = "up_to_date"
    """Theirs is already contained in ours: nothing to merge."""
    FAST_FORWARD = "fast_forward"
    """Ours is an ancestor of theirs: the target can move to theirs."""
    MERGE = "merge"
    """Histories diverged: a three-way merge against ``base`` is needed."""
    UNRELATED = "unrelated"
    """No common ancestor: merge against the empty tree."""


class VirtualBaseBuilder(Protocol):
    """Builds a synthetic merge of two Revisions and returns its Revision id.

    Called with the merge base of the pair (``None`` when they share no
    ancestor). Implemented by the merge engine; the result is never committed.
    """

    def __call__(self, base: bytes | None, ours: bytes, theirs: bytes) -> bytes: ...


@dataclass(frozen=True, slots=True)
class VirtualRevision:
    """A synthetic Revision produced while building a virtual merge base."""

    id: bytes
    parents: tuple[bytes, bytes]


@dataclass(frozen=True, slots=True)
class MergeBaseResult:
    status: MergeStatus
    base: bytes | None
    """Revision to use as merge base: a real ancestor, a virtual Revision, or ``None``."""
    common_ancestors: tuple[bytes, ...]
    """The lowest common ancestors, ordered by (generation, id)."""
    virtual_revisions: tuple[VirtualRevision, ...] = ()
    """Synthetic Revisions built for criss-cross histories, in creation order."""

    @property
    def virtual(self) -> bool:
        return bool(self.virtual_revisions)


def merge_base(
    graph: RevisionGraph,
    ours: bytes,
    theirs: bytes,
    build_virtual: VirtualBaseBuilder | None = None,
) -> MergeBaseResult:
    """Determine how to merge ``theirs`` (source head) into ``ours`` (target head).

    With several lowest common ancestors (criss-cross history) they are merged
    pairwise and recursively into a virtual base through ``build_virtual``.
    """
    common = lowest_common_ancestors(graph, ours, theirs)
    if theirs in common:
        return MergeBaseResult(MergeStatus.UP_TO_DATE, theirs, common)
    if ours in common:
        return MergeBaseResult(MergeStatus.FAST_FORWARD, ours, common)
    if not common:
        return MergeBaseResult(MergeStatus.UNRELATED, None, common)
    if len(common) == 1:
        return MergeBaseResult(MergeStatus.MERGE, common[0], common)
    if build_virtual is None:
        raise MergeBaseError("several lowest common ancestors and no virtual base builder")
    overlay = _OverlayGraph(graph)
    base = _virtual_base(overlay, common, build_virtual)
    return MergeBaseResult(MergeStatus.MERGE, base, common, tuple(overlay.added))


def _virtual_base(
    graph: _OverlayGraph, ancestors: Sequence[bytes], build: VirtualBaseBuilder
) -> bytes:
    merged = ancestors[0]
    for following in ancestors[1:]:
        inner = lowest_common_ancestors(graph, merged, following)
        if not inner:
            base: bytes | None = None
        elif len(inner) == 1:
            base = inner[0]
        else:
            base = _virtual_base(graph, inner, build)
        virtual = build(base, merged, following)
        graph.add_virtual(virtual, (merged, following))
        merged = virtual
    return merged


class _OverlayGraph:
    """A graph extended with the virtual Revisions of one merge-base computation."""

    def __init__(self, base: RevisionGraph) -> None:
        self._base = base
        self._virtual: dict[bytes, tuple[tuple[bytes, bytes], int]] = {}
        self.added: list[VirtualRevision] = []

    def add_virtual(self, revision: bytes, parents: tuple[bytes, bytes]) -> None:
        if revision in self._virtual:
            raise MergeBaseError("virtual base builder returned a duplicate Revision id")
        generation = next_generation(self.generation(p) for p in parents)
        self._virtual[revision] = (parents, generation)
        self.added.append(VirtualRevision(revision, parents))

    def parents(self, revision_id: bytes) -> Sequence[bytes]:
        entry = self._virtual.get(revision_id)
        return entry[0] if entry is not None else self._base.parents(revision_id)

    def generation(self, revision_id: bytes) -> int:
        entry = self._virtual.get(revision_id)
        return entry[1] if entry is not None else self._base.generation(revision_id)
