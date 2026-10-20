"""A two-Kind Domain with a few hundred Records, merged end to end on in-memory stores."""

from __future__ import annotations

from lorebase_core.merge import (
    ConflictKind,
    Resolution,
    ResolutionChoice,
    apply_resolutions,
)
from lorebase_core.model import Link, Record
from lorebase_core.tree import kind_partition, relation_partition, tree_count, tree_entries
from lorebase_core.validate import validate_link, validate_record

from .support import (
    NAME,
    PART,
    QTY,
    SUPPLIED_BY,
    SUPPLIER,
    Edit,
    Repo,
    add_attribute,
    attempt,
    attribute,
    base_schema,
    ident,
    merged,
    part,
    replace_kind,
    supplied_by,
    supplier,
)

SEGMENT = ident("att", 81)
SUPPLIERS, PARTS = 200, 150


def _supplier(n: int, name: str | None = None, segment: str | None = None) -> Record:
    values = {} if segment is None else {SEGMENT: segment}
    return supplier(f"S-{n:03d}", name, vat=f"IT{n:05d}", **values)


def _part(n: int, name: str | None = None) -> Record:
    return part(f"P-{n:03d}", name, qty=str(n))


def test_end_to_end_merge_with_resolutions() -> None:
    repo = Repo()
    schema = base_schema()
    data: list[Record | Link] = [_supplier(n) for n in range(SUPPLIERS)]
    data += [_part(n) for n in range(PARTS)]
    data += [supplied_by(f"P-{n:03d}", f"S-{n % SUPPLIERS:03d}") for n in range(PARTS)]
    base = repo.commit("base", Edit(schema=schema, put=data))

    # Ours: renames the Kind, adds a required Attribute with a default and backfills it,
    # renames 40 Suppliers and deletes 10 Parts with their Links.
    segmented = add_attribute(
        replace_kind(schema, SUPPLIER, api_name="Vendor"),
        SUPPLIER,
        attribute(SEGMENT, "segment", required=True, default="standard"),
    )
    ours_edit = Edit(schema=segmented).upsert(
        *(_supplier(n, "Ours" if n < 40 else None, "standard") for n in range(SUPPLIERS))
    )
    ours_edit.remove(*(_part(n) for n in range(10)))
    ours_edit.remove(*(supplied_by(f"P-{n:03d}", f"S-{n:03d}") for n in range(10)))
    ours = repo.commit("ours", ours_edit, base)

    # Theirs: changes the quantity of 5 Parts that ours deleted, renames other Suppliers,
    # adds 60 Suppliers and 40 Parts linked to them.
    theirs_edit = Edit().upsert(*(_part(n, "Changed") for n in range(5)))
    theirs_edit.upsert(*(_supplier(n, "Theirs") for n in range(40, 90)))
    theirs_edit.upsert(*(_supplier(n) for n in range(SUPPLIERS, SUPPLIERS + 60)))
    theirs_edit.upsert(*(_part(n) for n in range(PARTS, PARTS + 40)))
    theirs_edit.upsert(
        *(
            supplied_by(f"P-{n:03d}", f"S-{SUPPLIERS + n - PARTS:03d}")
            for n in range(PARTS, PARTS + 40)
        )
    )
    theirs = repo.commit("theirs", theirs_edit, base)

    first = attempt(repo.merge(ours, theirs))
    assert {c.kind for c in first.conflicts} == {ConflictKind.DELETE_MODIFY}
    assert sorted(c.key or "" for c in first.conflicts) == [f"P-{n:03d}" for n in range(5)]
    assert [(a.kind, a.count) for a in first.auto_resolutions] == [(ConflictKind.MISSING_VALUE, 60)]

    keep_deleted = [
        Resolution(fingerprint=c.fingerprint, choice=ResolutionChoice.OURS, resolver="usr_1")
        for c in first.conflicts
    ]
    result = merged(apply_resolutions(first, keep_deleted))

    root = result.root
    store = repo.store
    assert tree_count(store, root, kind_partition(SUPPLIER)) == SUPPLIERS + 60
    assert tree_count(store, root, kind_partition(PART)) == PARTS - 10 + 40
    assert tree_count(store, root, relation_partition(SUPPLIED_BY)) == PARTS - 10 + 40

    merged_schema = repo.schema(root)
    assert merged_schema.kinds[SUPPLIER].api_name == "Vendor"
    renamed = repo.values(root, SUPPLIER, "S-050") or {}
    assert (renamed[NAME], renamed[SEGMENT]) == ("Theirs", "standard")
    assert (repo.values(root, SUPPLIER, "S-010") or {})[NAME] == "Ours"
    assert (repo.values(root, SUPPLIER, "S-210") or {})[SEGMENT] == "standard"
    assert repo.values(root, PART, "P-003") is None
    assert (repo.values(root, PART, "P-160") or {})[QTY] == "160"

    # Invariant I1 holds for the whole merged tree.
    for partition in (kind_partition(SUPPLIER), kind_partition(PART)):
        for _, digest in tree_entries(store, root, partition):
            record = Record.model_validate(repo.objects.get(digest))
            assert validate_record(merged_schema, record) == []
    for _, digest in tree_entries(store, root, relation_partition(SUPPLIED_BY)):
        assert validate_link(merged_schema, Link.model_validate(repo.objects.get(digest))) == []

    assert result.stats.changes == len(result.changes)
    # New and renamed Suppliers, new Parts and their Links; kept deletions are not changes.
    assert result.stats.changes == 60 + 50 + 40 + 40
