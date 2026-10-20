"""Attribute-level data merge under the merged schema (design 6.8 step 3)."""

from __future__ import annotations

from collections.abc import Mapping

from lorebase_core.merge import MergeResult
from lorebase_core.tree import kind_partition
from lorebase_core.types import TypeSpec

from .support import (
    COUNTRY,
    NAME,
    PART,
    RATING,
    SUPPLIER,
    VAT,
    Edit,
    Repo,
    base_schema,
    fork,
    merged,
    part,
    remove_attribute,
    replace_attribute,
    supplier,
)


def test_different_attributes_of_one_record_merge() -> None:
    scenario = fork(
        Edit().upsert(supplier("S-1", "Ours", vat="IT001", country="IT", rating="3")),
        Edit().upsert(supplier("S-1", vat="IT001", country="DE", rating="3")),
    )
    result = merged(scenario.merge())
    values = scenario.repo.values(result.root, SUPPLIER, "S-1")
    assert values is not None
    assert (values[NAME], values[COUNTRY]) == ("Ours", "DE")


def test_one_sided_changes_are_taken() -> None:
    scenario = fork(Edit().upsert(part("P-9")), Edit().remove(part("P-3")).upsert(supplier("S-9")))
    result = merged(scenario.merge())
    repo = scenario.repo
    assert repo.values(result.root, PART, "P-9") is not None
    assert repo.values(result.root, PART, "P-3") is None
    assert repo.values(result.root, SUPPLIER, "S-9") is not None
    assert result.result is MergeResult.MERGED
    assert result.parents == (scenario.ours, scenario.theirs)


def test_values_written_on_one_side_are_cast_to_a_type_changed_on_the_other() -> None:
    decimal = TypeSpec.decimal(10, 2)
    retyped = replace_attribute(base_schema(), SUPPLIER, RATING, type=decimal)
    scenario = fork(
        Edit(schema=retyped).upsert(supplier("S-1", vat="IT001", country="IT", rating="3.00")),
        Edit().upsert(supplier("S-2", vat="IT002", country="IT", rating="4")),
    )
    result = merged(scenario.merge())
    repo = scenario.repo
    assert (repo.values(result.root, SUPPLIER, "S-2") or {})[RATING] == "4.00"
    assert (repo.values(result.root, SUPPLIER, "S-1") or {})[RATING] == "3.00"


def test_removed_attribute_without_new_values_merges_silently() -> None:
    scenario = fork(
        Edit(schema=remove_attribute(base_schema(), SUPPLIER, RATING)).upsert(
            supplier("S-1", vat="IT001", country="IT")
        ),
        Edit().upsert(supplier("S-2", "Renamed", vat="IT002", country="IT")),
    )
    result = merged(scenario.merge())
    values = scenario.repo.values(result.root, SUPPLIER, "S-1") or {}
    assert RATING not in values


def test_changeset_is_against_ours_with_provenance_from_theirs() -> None:
    scenario = fork(
        Edit().upsert(supplier("S-1", "Ours", vat="IT001", country="IT", rating="3")),
        Edit().upsert(
            supplier("S-1", vat="IT009", country="IT", rating="3"),
            supplier("S-9"),
        ),
    )
    lineage = {NAME: "prv_name", VAT: "prv_vat"}

    def provenance(revision: bytes, partition: str, key: str) -> Mapping[str, str]:
        assert revision == scenario.theirs
        return lineage

    result = merged(scenario.merge(provenance=provenance))
    ours_root = scenario.repo.roots[scenario.ours]
    by_key = {(e.partition, e.key): e for e in result.changes}
    s1 = by_key[(kind_partition(SUPPLIER), "S-1")]
    assert s1.old_hash == _hash(scenario.repo, ours_root, "S-1")
    assert dict(s1.provenance) == {VAT: "prv_vat"}
    s9 = by_key[(kind_partition(SUPPLIER), "S-9")]
    assert s9.old_hash is None
    assert dict(s9.provenance) == lineage
    assert result.stats.changes == 2


def _hash(repo: Repo, root: bytes, key: str) -> bytes | None:
    from lorebase_core.tree import tree_get

    return tree_get(repo.store, root, kind_partition(SUPPLIER), key)


def test_unrelated_histories_merge_against_the_empty_tree() -> None:
    repo = Repo()
    ours = repo.commit("ours", Edit(schema=base_schema(), put=[supplier("S-1")]))
    theirs = repo.commit("theirs", Edit(schema=base_schema(), put=[supplier("S-2")]))
    result = merged(repo.merge(ours, theirs))
    assert result.base is None
    assert repo.keys(result.root, kind_partition(SUPPLIER)) == {"S-1", "S-2"}
