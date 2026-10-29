"""A preserved base replaces values of the merge base (design 9.1): a value one side holds
and the other changed from an older value merges as a change of both sides."""

from __future__ import annotations

from lorebase_core.merge import ConflictKind, MergeResult
from lorebase_core.tree import kind_partition

from .support import (
    NAME,
    RATING,
    SUPPLIER,
    Edit,
    Repo,
    attempt,
    base_records,
    base_schema,
    merged,
    supplier,
)


def branch_from_target() -> tuple[Repo, bytes, bytes]:
    """`main` holds a person's name for S-1; the Source branch, cut from it, writes the new
    upstream name. The merge base is `main` itself."""
    repo = Repo()
    main = repo.commit("main", Edit(schema=base_schema(), put=list(base_records())))
    edited = repo.commit(
        "edited",
        Edit().upsert(supplier("S-1", "Person", vat="IT001", country="IT", rating="3")),
        main,
    )
    source = repo.commit(
        "source",
        Edit().upsert(supplier("S-1", "Upstream", vat="IT001", country="IT", rating="4")),
        edited,
    )
    return repo, edited, source


def test_without_overrides_the_source_branch_fast_forwards() -> None:
    repo, ours, theirs = branch_from_target()
    result = merged(repo.merge(ours, theirs))
    assert result.result is MergeResult.FAST_FORWARD
    assert (repo.values(result.root, SUPPLIER, "S-1") or {})[NAME] == "Upstream"


def test_an_override_turns_the_overwrite_into_a_value_conflict() -> None:
    repo, ours, theirs = branch_from_target()
    overrides = {(kind_partition(SUPPLIER), "S-1"): {NAME: "Supplier S-1"}}
    outcome = attempt(repo.merge(ours, theirs, base_overrides=overrides))
    assert outcome.virtual_base
    [conflict] = outcome.conflicts
    assert conflict.kind is ConflictKind.ATTRIBUTE_VALUE
    assert (conflict.key, conflict.attribute) == ("S-1", NAME)
    # The rating changed upstream only: it is not part of the Conflict.
    assert all(c.attribute != RATING for c in outcome.conflicts)


def test_an_override_matching_the_base_changes_nothing() -> None:
    repo, ours, theirs = branch_from_target()
    overrides = {(kind_partition(SUPPLIER), "S-1"): {NAME: "Person"}}
    assert merged(repo.merge(ours, theirs, base_overrides=overrides)).result is (
        MergeResult.FAST_FORWARD
    )


def test_an_absent_base_value_conflicts_too() -> None:
    repo, ours, theirs = branch_from_target()
    overrides = {(kind_partition(SUPPLIER), "S-1"): {NAME: None}}
    [conflict] = attempt(repo.merge(ours, theirs, base_overrides=overrides)).conflicts
    assert conflict.kind is ConflictKind.ATTRIBUTE_VALUE
    assert conflict.base is None


def test_a_deletion_over_a_preserved_base_is_a_delete_modify_conflict() -> None:
    repo = Repo()
    main = repo.commit("main", Edit(schema=base_schema(), put=list(base_records())))
    source = repo.commit("source", Edit().remove(supplier("S-3")), main)
    assert merged(repo.merge(main, source)).result is MergeResult.FAST_FORWARD
    overrides = {(kind_partition(SUPPLIER), "S-3"): {NAME: "Old name"}}
    [conflict] = attempt(repo.merge(main, source, base_overrides=overrides)).conflicts
    assert conflict.kind is ConflictKind.DELETE_MODIFY


def test_a_record_the_base_lacks_is_added_from_the_override() -> None:
    repo = Repo()
    main = repo.commit("main", Edit(schema=base_schema(), put=list(base_records())))
    deleted = repo.commit("deleted", Edit().remove(supplier("S-3")), main)
    source = repo.commit("source", Edit().upsert(supplier("S-3", "Upstream")), deleted)
    assert merged(repo.merge(deleted, source)).result is MergeResult.FAST_FORWARD
    overrides = {(kind_partition(SUPPLIER), "S-3"): {NAME: "Old name"}}
    [conflict] = attempt(repo.merge(deleted, source, base_overrides=overrides)).conflicts
    assert conflict.kind is ConflictKind.DELETE_MODIFY
