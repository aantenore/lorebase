"""The delta of a run against a fake branch state: added, changed, removed, Links, batches."""

from __future__ import annotations

from typing import Any

import pytest

from lorebase_core.model import (
    IdPrefix,
    LocalEdits,
    ProvenanceEntry,
    Record,
    Schema,
    SourceSyncOrigin,
    new_id,
)
from lorebase_core.validate import LinkDelete, LinkUpsert, RecordDelete, RecordUpsert
from lorebase_scribe.sources.delta import (
    ChangeGroup,
    CurrentState,
    EditRules,
    Outcome,
    Setter,
    StoredRecord,
    WrittenState,
    batches,
    compute_delta,
    content_hash,
    value_digest,
)
from lorebase_scribe.sources.mapping import MappedRow, RowMapper
from lorebase_store.repositories import WrittenRow

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


# Corrections that stick (design 9.1) ------------------------------------------------------------


def status_id(schema: Schema) -> str:
    kind = schema.kind_by_api_name("PurchaseOrder")
    assert kind is not None
    found = kind.attribute_by_api_name("status")
    assert found is not None
    return found.id


def last_written(mapper: RowMapper, row: dict[str, Any], revision: bytes = b"\x01" * 32) -> Any:
    """The state a merged run with `row` leaves."""
    mapped = mapper.map(row, "x")
    written = {a: value_digest(mapped.values.get(a)) for a in mapper.owned_attributes}
    return WrittenRow(mapper.kind.id, mapped.key, written, revision, "srn_0")


PERSON = Setter("operation", "usr_editor")


def edited_state(
    mapper: RowMapper, schema: Schema, setter: Setter = PERSON
) -> tuple[str, CurrentState, WrittenState]:
    """The branch holds a person's status over what the Source last wrote."""
    key, record = stored(mapper, ORDER)
    status = status_id(schema)
    values = {**record.values, status: "cancelled"}
    edited = Record(kind=mapper.kind.id, key=key, values=values)
    current = CurrentState(
        records={key: StoredRecord(content_hash(edited), values)},
        foreign={(key, status): setter},
    )
    return key, current, WrittenState(records={key: last_written(mapper, ORDER)})


def test_an_unchanged_upstream_value_leaves_a_correction(
    mapper: RowMapper, supply_schema: Schema
) -> None:
    _key, current, written = edited_state(mapper, supply_schema)
    delta = compute_delta(
        mapper,
        [mapper.map(ORDER, "r")],
        current,
        written,
        delete_missing=True,
        provenance=provenance,
    )
    assert delta.changed == []
    assert delta.local_edits == []
    assert delta.seeded == 0


def test_an_upstream_change_over_a_correction_is_a_local_edit(
    mapper: RowMapper, supply_schema: Schema
) -> None:
    key, current, written = edited_state(mapper, supply_schema)
    status = status_id(supply_schema)
    row = mapper.map({**ORDER, "state": "shipped"}, "r")
    review = compute_delta(
        mapper, [row], current, written, delete_missing=True, provenance=provenance
    )
    assert [(e.key, e.attribute, e.outcome) for e in review.local_edits] == [
        (key, status, Outcome.CONFLICT)
    ]
    upsert = review.groups[0].changes[0].change
    assert isinstance(upsert, RecordUpsert)
    assert upsert.record.values[status] == "shipped"  # theirs on the Source branch
    [state] = [s for s in review.states if s.element == mapper.kind.id]
    assert state.preserve == (status,)
    # Values upstream did not change keep the Revision that holds them.
    assert set(state.kept) == set(mapper.owned_attributes) - {status}

    wins = compute_delta(
        mapper,
        [row],
        current,
        written,
        delete_missing=True,
        provenance=provenance,
        rules=EditRules(LocalEdits.SOURCE_WINS),
    )
    assert [e.outcome for e in wins.local_edits] == [Outcome.OVERWRITTEN]
    assert all(not s.preserve for s in wins.states)

    keep = EditRules(LocalEdits.KEEP)
    kept = compute_delta(
        mapper, [row], current, written, delete_missing=True, provenance=provenance, rules=keep
    )
    assert [e.outcome for e in kept.local_edits] == [Outcome.KEPT]
    _key, extracted, _written = edited_state(mapper, supply_schema, Setter("extraction", "svc"))
    overwritten = compute_delta(
        mapper, [row], extracted, written, delete_missing=True, provenance=provenance, rules=keep
    )
    assert [e.outcome for e in overwritten.local_edits] == [Outcome.OVERWRITTEN]
    # Under auto_with_authority the Source's own Attributes are always overwritten.
    authority = EditRules(LocalEdits.REVIEW, frozenset({status}))
    owned = compute_delta(
        mapper,
        [row],
        current,
        written,
        delete_missing=True,
        provenance=provenance,
        rules=authority,
    )
    assert [e.outcome for e in owned.local_edits] == [Outcome.OVERWRITTEN]


def test_upstream_catching_up_with_a_correction_is_no_change(
    mapper: RowMapper, supply_schema: Schema
) -> None:
    _key, current, written = edited_state(mapper, supply_schema)
    row = mapper.map({**ORDER, "state": "CANCELLED"}, "r")  # lowered by the mapping
    delta = compute_delta(
        mapper, [row], current, written, delete_missing=True, provenance=provenance
    )
    assert delta.changed == []
    assert delta.local_edits == []
    [state] = [s for s in delta.states if s.element == mapper.kind.id]
    assert state.written[status_id(supply_schema)] == value_digest("cancelled")


def test_a_deletion_of_a_corrected_record_is_a_local_edit(
    mapper: RowMapper, supply_schema: Schema
) -> None:
    key, current, written = edited_state(mapper, supply_schema)
    delta = compute_delta(mapper, [], current, written, delete_missing=True, provenance=provenance)
    assert delta.removed == [key]
    assert [(e.attribute, e.outcome) for e in delta.local_edits] == [(None, Outcome.CONFLICT)]
    [state] = [s for s in delta.states if s.element == mapper.kind.id]
    assert state.deleted
    assert state.preserve == (status_id(supply_schema),)
    wins = compute_delta(
        mapper,
        [],
        current,
        written,
        delete_missing=True,
        provenance=provenance,
        rules=EditRules(LocalEdits.SOURCE_WINS),
    )
    assert [e.outcome for e in wins.local_edits] == [Outcome.DELETED]


def test_seeding_compares_values_others_set(mapper: RowMapper, supply_schema: Schema) -> None:
    _key, current, _written = edited_state(mapper, supply_schema)
    same = compute_delta(
        mapper,
        [mapper.map({**ORDER, "state": "cancelled"}, "r")],
        current,
        delete_missing=True,
        provenance=provenance,
    )
    assert (same.seeded, same.changed, same.local_edits) == (1, [], [])
    other = compute_delta(
        mapper, [mapper.map(ORDER, "r")], current, delete_missing=True, provenance=provenance
    )
    assert [e.attribute for e in other.local_edits] == [status_id(supply_schema)]


def test_links_follow_upstream_changes_only(mapper: RowMapper, supply_schema: Schema) -> None:
    key, record = stored(mapper, ORDER)
    for_part = relation(supply_schema, "for_part")
    delivered = relation(supply_schema, "delivered_to")
    # A person removed the Link to P-10001 and added one to P-20000.
    current = CurrentState(
        records={key: record},
        links={for_part: {key: frozenset({"P-20000"})}, delivered: {key: frozenset({"PL-10"})}},
    )
    last = {
        for_part: {key: WrittenRow(for_part, key, {"P-10001": value_digest("P-10001")}, None, "x")},
        delivered: {key: WrittenRow(delivered, key, {"PL-10": value_digest("PL-10")}, None, "x")},
    }
    written = WrittenState(records={key: last_written(mapper, ORDER)}, links=last)
    unchanged = compute_delta(
        mapper,
        [mapper.map(ORDER, "r")],
        current,
        written,
        delete_missing=True,
        provenance=provenance,
    )
    assert unchanged.groups == []
    moved = compute_delta(
        mapper,
        [mapper.map({**ORDER, "part_no": "P-10002"}, "r")],
        current,
        written,
        delete_missing=True,
        provenance=provenance,
    )
    changes = [c.change for c in moved.groups[0].changes]
    assert [type(c) for c in changes] == [LinkUpsert]  # P-20000 stays, P-10001 is gone already
