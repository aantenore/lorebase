"""Criss-cross histories merge against a recursive virtual base (AC-13)."""

from __future__ import annotations

from lorebase_core.merge import ConflictKind, MergeAttempt
from lorebase_core.revision import lowest_common_ancestors

from .support import (
    NAME,
    PART,
    PART_NAME,
    SUPPLIER,
    Edit,
    Repo,
    base_records,
    base_schema,
    merged,
    part,
    supplier,
)


def criss_cross() -> tuple[Repo, bytes, bytes]:
    """A -> B, C; D = merge(B, C); E = merge(C, B); D and E then diverge again.

    B renames S-1 and C renames P-1; both merges contain both renames. D renames S-1
    again and E renames P-1 again.
    """
    repo = Repo()
    a = repo.commit("A", Edit(schema=base_schema(), put=list(base_records())))
    b = repo.commit(
        "B", Edit().upsert(supplier("S-1", "B", vat="IT001", country="IT", rating="3")), a
    )
    c = repo.commit("C", Edit().upsert(part("P-1", "C", qty="10")), a)
    both = Edit().upsert(
        supplier("S-1", "B", vat="IT001", country="IT", rating="3"), part("P-1", "C", qty="10")
    )
    d = repo.commit("D", both, b, c)
    e = repo.commit("E", both, c, b)
    d2 = repo.commit(
        "D2", Edit().upsert(supplier("S-1", "D", vat="IT001", country="IT", rating="3")), d
    )
    e2 = repo.commit("E2", Edit().upsert(part("P-1", "E", qty="10")), e)
    return repo, d2, e2


def test_criss_cross_has_two_lowest_common_ancestors() -> None:
    repo, ours, theirs = criss_cross()
    assert len(lowest_common_ancestors(repo.graph, ours, theirs)) == 2


def test_virtual_base_makes_the_merge_clean() -> None:
    repo, ours, theirs = criss_cross()
    result = merged(repo.merge(ours, theirs))
    assert result.virtual_base
    assert (repo.values(result.root, SUPPLIER, "S-1") or {})[NAME] == "D"
    assert (repo.values(result.root, PART, "P-1") or {})[PART_NAME] == "E"


def test_without_the_virtual_base_the_same_merge_conflicts() -> None:
    repo, ours, theirs = criss_cross()
    a = next(r for r, root in repo.roots.items() if repo.graph.generation(r) == 1)
    outcome = repo.merge(ours, theirs, base=a)
    assert isinstance(outcome, MergeAttempt)
    assert {c.kind for c in outcome.conflicts} == {ConflictKind.ATTRIBUTE_VALUE}


def test_conflicting_ancestors_fall_back_to_their_base_in_the_virtual_base() -> None:
    repo = Repo()
    a = repo.commit("A", Edit(schema=base_schema(), put=list(base_records())))
    b = repo.commit(
        "B", Edit().upsert(supplier("S-1", "B", vat="IT001", country="IT", rating="3")), a
    )
    c = repo.commit(
        "C", Edit().upsert(supplier("S-1", "C", vat="IT001", country="IT", rating="3")), a
    )
    resolved = Edit().upsert(supplier("S-1", "BC", vat="IT001", country="IT", rating="3"))
    d = repo.commit("D", resolved, b, c)
    e = repo.commit("E", resolved, c, b)
    d2 = repo.commit("D2", Edit().upsert(supplier("S-2", "D2", vat="IT002", country="IT")), d)
    result = merged(repo.merge(d2, e))
    assert result.virtual_base
    assert (repo.values(result.root, SUPPLIER, "S-1") or {})[NAME] == "BC"
    assert (repo.values(result.root, SUPPLIER, "S-2") or {})[NAME] == "D2"
