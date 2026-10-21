"""The delta of a run against a fake branch state: added, changed, removed, Links, batches."""

from __future__ import annotations

from typing import Any

import pytest

from lorebase_core.model import (
    IdPrefix,
    ProvenanceEntry,
    Schema,
    SourceSyncOrigin,
    new_id,
)
from lorebase_core.validate import LinkDelete, LinkUpsert, RecordDelete, RecordUpsert
from lorebase_scribe.sources.delta import (
    ChangeGroup,
    CurrentState,
    StoredRecord,
    batches,
    compute_delta,
    content_hash,
)
from lorebase_scribe.sources.mapping import MappedRow, RowMapper

ORDER = {
    "po_no": "PO-200001",
    "vendor_id": "S-1005",
    "state": "issued",
    "ordered": "2025-11-16",
    "due": "2026-01-10",
    "qty": "2500",
    "price": "11.62",
    "currency": "EUR",
    "part_no": "P-10001",
    "plant_code": "PL-10",
    "deleted_at": "",
}


def provenance(row: MappedRow) -> ProvenanceEntry:
    return ProvenanceEntry(
        id=new_id(IdPrefix.PROVENANCE),
        origin=SourceSyncOrigin(
            source_id="src_01KEZF7C00PMKRJJ98PAYYB4SX", run_id="srn_1", row_locator=row.locator
        ),
        actor="svc_scribe",
        at="2026-10-21T08:00:00.000000Z",
    )


@pytest.fixture
def mapper(supply_schema: Schema) -> RowMapper:
    found = supply_schema.source_by_api_name("erp.purchase_orders")
    assert found is not None
    return RowMapper(supply_schema, found)


def relation(schema: Schema, name: str) -> str:
    found = schema.relation_by_api_name(name)
    assert found is not None
    return found[0].id


def stored(mapper: RowMapper, row: dict[str, Any]) -> tuple[str, StoredRecord]:
    mapped = mapper.map(row, "x")
    record = mapper.record(mapped, None)
    return record.key, StoredRecord(content_hash(record), record.values)


def test_first_run_adds_records_and_links(mapper: RowMapper, supply_schema: Schema) -> None:
    rows = [mapper.map(ORDER, "orders:1")]
    delta = compute_delta(mapper, rows, CurrentState(), delete_missing=True, provenance=provenance)
    assert delta.counts() == {
        "added": 1,
        "changed": 0,
        "removed": 0,
        "unchanged": 0,
        "links_added": 2,
        "links_removed": 0,
    }
    (group,) = delta.groups
    ops = [type(c.change) for c in group.changes]
    assert ops == [RecordUpsert, LinkUpsert, LinkUpsert]
    entries = {c.provenance.id for c in group.changes if c.provenance is not None}
    assert len(entries) == 1  # one provenance entry per row
    (entry,) = {c.provenance for c in group.changes if c.provenance is not None}
    assert entry.origin.row_locator == "orders:1"  # type: ignore[union-attr]


def test_same_content_is_no_change(mapper: RowMapper, supply_schema: Schema) -> None:
    key, record = stored(mapper, ORDER)
    current = CurrentState(
        records={key: record},
        owned=frozenset({key}),
        links={
            relation(supply_schema, "for_part"): {key: frozenset({"P-10001"})},
            relation(supply_schema, "delivered_to"): {key: frozenset({"PL-10"})},
        },
    )
    delta = compute_delta(
        mapper, [mapper.map(ORDER, "r")], current, delete_missing=True, provenance=provenance
    )
    assert delta.groups == []
    assert delta.unchanged == 1


def test_changed_value_and_moved_link(mapper: RowMapper, supply_schema: Schema) -> None:
    key, record = stored(mapper, ORDER)
    for_part = relation(supply_schema, "for_part")
    current = CurrentState(
        records={key: record},
        owned=frozenset({key}),
        links={for_part: {key: frozenset({"P-10001"})}},
    )
    changed = {**ORDER, "state": "shipped", "part_no": "P-10002", "plant_code": ""}
    delta = compute_delta(
        mapper, [mapper.map(changed, "r")], current, delete_missing=True, provenance=provenance
    )
    assert delta.changed == [key]
    assert (delta.links_added, delta.links_removed) == (1, 1)
    changes = [c.change for c in delta.groups[0].changes]
    assert isinstance(changes[0], RecordUpsert)
    assert any(isinstance(c, LinkDelete) and c.to_key == "P-10001" for c in changes)
    assert any(isinstance(c, LinkUpsert) and c.link.to_key == "P-10002" for c in changes)


def test_snapshot_removes_owned_keys_only(mapper: RowMapper, supply_schema: Schema) -> None:
    key, record = stored(mapper, ORDER)
    other_key, other = stored(mapper, {**ORDER, "po_no": "PO-200002"})
    manual_key, manual = stored(mapper, {**ORDER, "po_no": "PO-200003"})
    for_part = relation(supply_schema, "for_part")
    current = CurrentState(
        records={key: record, other_key: other, manual_key: manual},
        owned=frozenset({key, other_key}),
        links={for_part: {other_key: frozenset({"P-10001"})}},
    )
    rows = [mapper.map(ORDER, "r")]
    delta = compute_delta(mapper, rows, current, delete_missing=True, provenance=provenance)
    assert delta.removed == [other_key]  # the manual Record is not owned
    removal = [c.change for c in delta.groups[-1].changes]
    assert isinstance(removal[0], LinkDelete)
    assert isinstance(removal[-1], RecordDelete)
    kept = compute_delta(mapper, rows, current, delete_missing=False, provenance=provenance)
    assert kept.removed == []


def test_tombstones_remove_existing_records(mapper: RowMapper) -> None:
    key, record = stored(mapper, ORDER)
    rows = [
        mapper.map({**ORDER, "deleted_at": "2026-01-01"}, "r1"),
        mapper.map({**ORDER, "po_no": "PO-299999", "deleted_at": "2026-01-01"}, "r2"),
    ]
    delta = compute_delta(
        mapper,
        rows,
        CurrentState(records={key: record}),
        delete_missing=False,
        provenance=provenance,
    )
    assert delta.removed == [key]


def test_later_rows_of_a_key_win(mapper: RowMapper) -> None:
    rows = [mapper.map(ORDER, "r1"), mapper.map({**ORDER, "state": "received"}, "r2")]
    delta = compute_delta(mapper, rows, CurrentState(), delete_missing=True, provenance=provenance)
    upsert = delta.groups[0].changes[0]
    assert isinstance(upsert.change, RecordUpsert)
    assert upsert.provenance is not None
    assert upsert.provenance.origin.row_locator == "r2"  # type: ignore[union-attr]


def test_batches_never_split_a_group() -> None:
    def group(size: int) -> ChangeGroup:
        change = RecordDelete(kind="knd_01KEZF7C00M11T0RH8CAVBW897", key="k")
        from lorebase_scribe.sources.delta import Change

        return ChangeGroup("k", tuple(Change(change) for _ in range(size)))

    packed = batches([group(3), group(3), group(1), group(5)], 4)
    assert [[len(g.changes) for g in batch] for batch in packed] == [[3], [3, 1], [5]]
