"""Merge attempts over the in-memory core stores: stored resolutions, staleness, provenance."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from lorebase_core.canonical import ObjectType
from lorebase_core.merge import (
    ConflictKind,
    InMemoryObjectStore,
    MergedTree,
    MergeInput,
    Resolution,
    ResolutionChoice,
    ResolutionError,
    put_schema,
)
from lorebase_core.model import Attribute, Domain, Kind, Record, Schema
from lorebase_core.revision import ChangesetEntry, InMemoryRevisionGraph
from lorebase_core.tree import InMemoryNodeStore, TreeChange, kind_partition, tree_apply
from lorebase_core.types import TypeSpec
from lorebase_store.materialize.rows import ELEMENT_PROVENANCE_KEY
from lorebase_store.merging import (
    Decision,
    check_resolution,
    complete_provenance,
    compute_attempt,
)

from .support import ident

SUPPLIER = ident("knd", 901)
CODE = ident("att", 911)
NAME = ident("att", 912)
VAT = ident("att", 913)
C = ResolutionChoice
S = TypeSpec.of("string")


def schema() -> Schema:
    attributes = {
        CODE: Attribute(id=CODE, api_name="code", type=S, required=True),
        NAME: Attribute(id=NAME, api_name="name", type=S),
        VAT: Attribute(id=VAT, api_name="vat", type=S, unique=True),
    }
    kind = Kind(id=SUPPLIER, api_name="Supplier", primary_key=(CODE,), attributes=attributes)
    return Schema(domain=Domain(id="merging"), kinds={SUPPLIER: kind})


def supplier(code: str, vat: str, name: str | None = None) -> Record:
    values: dict[str, Any] = {CODE: code, VAT: vat}
    if name is not None:
        values[NAME] = name
    return Record(kind=SUPPLIER, key=code, values=values)


class Repo:
    """Node store, object store and Revision graph in memory."""

    def __init__(self) -> None:
        self.store = InMemoryNodeStore()
        self.objects = InMemoryObjectStore()
        self.graph = InMemoryRevisionGraph()
        self.roots: dict[bytes, bytes] = {}

    def commit(self, label: str, records: list[Record], *parents: bytes) -> bytes:
        root = self.roots[parents[0]] if parents else None
        changes = [] if parents else list(put_schema(self.objects, schema()))
        changes.extend(
            TreeChange(
                kind_partition(r.kind), r.key, self.objects.put(ObjectType.RECORD, r.content())
            )
            for r in records
        )
        revision = hashlib.sha256(label.encode()).digest()
        self.graph.add(revision, parents)
        self.roots[revision] = tree_apply(self.store, root, changes)
        return revision

    def input(self, ours: bytes, theirs: bytes) -> MergeInput:
        return MergeInput(
            store=self.store,
            objects=self.objects,
            graph=self.graph,
            revision_root=self.roots.__getitem__,
            ours=ours,
            theirs=theirs,
        )


@pytest.fixture
def repo() -> Repo:
    return Repo()


def fork(repo: Repo) -> tuple[bytes, bytes, bytes]:
    """Base with two Suppliers; both sides set a different VAT on S-1."""
    base = repo.commit("base", [supplier("S-1", "IT001"), supplier("S-2", "IT002")])
    ours = repo.commit("ours", [supplier("S-1", "IT100")], base)
    theirs = repo.commit("theirs", [supplier("S-1", "IT200")], base)
    return base, ours, theirs


def test_open_conflict_without_resolutions(repo: Repo) -> None:
    _, ours, theirs = fork(repo)
    computed = compute_attempt(repo.input(ours, theirs), [])
    assert not computed.clean
    assert [s.conflict.kind for s in computed.conflicts] == [ConflictKind.ATTRIBUTE_VALUE]
    assert computed.open_count == 1
    assert computed.resolved_count == 0
    assert computed.unmatched == ()


def test_stored_resolution_resolves_and_is_reported(repo: Repo) -> None:
    _, ours, theirs = fork(repo)
    fingerprint = compute_attempt(repo.input(ours, theirs), []).conflicts[0].conflict.fingerprint
    resolution = Resolution(fingerprint=fingerprint, choice=C.THEIRS, resolver="usr_r")
    computed = compute_attempt(repo.input(ours, theirs), [resolution])
    assert computed.clean
    [state] = computed.conflicts
    assert not state.open
    assert state.resolution == resolution
    assert computed.applied == (resolution,)


def test_resolutions_converge_through_surfaced_conflicts(repo: Repo) -> None:
    _, ours, theirs = fork(repo)
    first = compute_attempt(repo.input(ours, theirs), []).conflicts[0].conflict
    duplicate = Resolution(fingerprint=first.fingerprint, choice=C.CUSTOM, value="IT002")
    second = compute_attempt(repo.input(ours, theirs), [duplicate])
    assert [(s.conflict.kind, s.open) for s in second.conflicts] == [
        (ConflictKind.ATTRIBUTE_VALUE, False),
        (ConflictKind.UNIQUENESS, True),
    ]
    uniqueness = second.conflicts[1]
    assert set(uniqueness.members) == {"S-1", "S-2"}
    fix = Resolution(
        fingerprint=uniqueness.conflict.fingerprint,
        choice=C.CUSTOM,
        values={"S-1": "IT009", "S-2": "IT002"},
    )
    final = compute_attempt(repo.input(ours, theirs), [fix, duplicate])
    assert final.clean
    assert {s.conflict.kind for s in final.conflicts} == {
        ConflictKind.ATTRIBUTE_VALUE,
        ConflictKind.UNIQUENESS,
    }
    assert all(not s.open for s in final.conflicts)


def test_resolution_that_leaves_the_conflict_is_rejected(repo: Repo) -> None:
    _, ours, theirs = fork(repo)
    first = compute_attempt(repo.input(ours, theirs), []).conflicts[0].conflict
    duplicate = Resolution(fingerprint=first.fingerprint, choice=C.CUSTOM, value="IT002")
    uniqueness = compute_attempt(repo.input(ours, theirs), [duplicate]).conflicts[1].conflict
    still = Resolution(
        fingerprint=uniqueness.fingerprint,
        choice=C.CUSTOM,
        values={"S-1": "IT002", "S-2": "IT002"},
    )
    computed = compute_attempt(repo.input(ours, theirs), [duplicate, still])
    rejected = [s for s in computed.conflicts if s.rejected]
    assert [s.conflict.kind for s in rejected] == [ConflictKind.UNIQUENESS]


def test_moved_target_keeps_matching_resolutions(repo: Repo) -> None:
    _, ours, theirs = fork(repo)
    fingerprint = compute_attempt(repo.input(ours, theirs), []).conflicts[0].conflict.fingerprint
    resolution = Resolution(fingerprint=fingerprint, choice=C.OURS)
    # An unrelated change on the target: the Conflict and its fingerprint stay the same.
    moved = repo.commit("ours-2", [supplier("S-3", "IT003")], ours)
    computed = compute_attempt(repo.input(moved, theirs), [resolution])
    assert computed.clean
    assert computed.unmatched == ()


def test_changed_conflict_leaves_its_resolution_unmatched(repo: Repo) -> None:
    _, ours, theirs = fork(repo)
    fingerprint = compute_attempt(repo.input(ours, theirs), []).conflicts[0].conflict.fingerprint
    resolution = Resolution(fingerprint=fingerprint, choice=C.OURS)
    # The target changes S-1 again: a new Conflict with a new fingerprint.
    moved = repo.commit("ours-2", [supplier("S-1", "IT150")], ours)
    computed = compute_attempt(repo.input(moved, theirs), [resolution])
    assert not computed.clean
    assert computed.unmatched == (fingerprint,)
    assert computed.conflicts[0].conflict.fingerprint != fingerprint
    assert computed.conflicts[0].open


def test_fast_forward_is_clean(repo: Repo) -> None:
    base = repo.commit("base", [supplier("S-1", "IT001")])
    theirs = repo.commit("theirs", [supplier("S-1", "IT200")], base)
    computed = compute_attempt(repo.input(base, theirs), [])
    assert isinstance(computed.outcome, MergedTree)
    assert computed.outcome.result.value == "fast_forward"
    assert computed.conflicts == ()


def test_check_resolution(repo: Repo) -> None:
    _, ours, theirs = fork(repo)
    first = compute_attempt(repo.input(ours, theirs), []).conflicts[0].conflict
    check_resolution(first, (), Resolution(fingerprint=first.fingerprint, choice=C.BASE))
    with pytest.raises(ResolutionError, match="does not resolve"):
        check_resolution(first, (), Resolution(fingerprint=first.fingerprint, choice=C.DROP))
    with pytest.raises(ResolutionError, match="another Conflict"):
        check_resolution(first, (), Resolution(fingerprint="0" * 64, choice=C.OURS))
    duplicate = Resolution(fingerprint=first.fingerprint, choice=C.CUSTOM, value="IT002")
    state = compute_attempt(repo.input(ours, theirs), [duplicate]).conflicts[1]
    partial = Resolution(
        fingerprint=state.conflict.fingerprint, choice=C.CUSTOM, values={"S-1": "IT009"}
    )
    # Uniqueness accepts new values for some of the Records only.
    check_resolution(state.conflict, state.members, partial)


def test_complete_provenance_attributes_every_change() -> None:
    objects = InMemoryObjectStore()
    old = objects.put(ObjectType.RECORD, supplier("S-1", "IT001", "Old").content())
    new = objects.put(ObjectType.RECORD, supplier("S-1", "IT100", "New").content())
    partition = kind_partition(SUPPLIER)
    entries = [
        ChangesetEntry(partition, "S-1", old, new, {NAME: "prv_theirs"}),
        ChangesetEntry(partition, "S-9", old, None, {}),
    ]
    decision = Decision(
        fingerprint="f" * 64,
        resolver="usr_r",
        choice=C.CUSTOM,
        partition=partition,
        key="S-1",
        attribute=VAT,
    )
    completed, used = complete_provenance(
        entries, objects, [decision], {"f" * 64: "prv_resolution"}, "prv_merge"
    )
    assert dict(completed[0].provenance) == {NAME: "prv_theirs", VAT: "prv_resolution"}
    assert dict(completed[1].provenance) == {ELEMENT_PROVENANCE_KEY: "prv_merge"}
    assert used == {"f" * 64}
