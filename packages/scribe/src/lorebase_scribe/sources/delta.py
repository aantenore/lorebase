"""The delta of a Source run against the current head of its branch (design 9.1 steps 2-3).

Pure: the current state is passed in. A row is `added` when its key is not on the branch,
`changed` when the content hash of its new Record differs from the stored one, and otherwise
unchanged (Lineage is not content, so rewriting the same values is no change). Links of the
mapped Relations follow the rows. `removed` keys are the owned keys the run did not see (when
deletions apply) plus the keys of tombstone rows that exist. The changes of one key are kept
together in a `ChangeGroup`, so a batch boundary never splits a Record from its Links.

A value whose row maps a validity period (`MappedRow.periods`) and that the change writes (new
or different from the stored one) gets a provenance entry of its own carrying the period; the
other values of the change share the row's entry (design 10.6).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from pydantic import JsonValue

from lorebase_core.canonical import ObjectType, normalize, object_hash
from lorebase_core.model import Link, ProvenanceEntry, Record
from lorebase_core.validate import DataChange, LinkDelete, LinkUpsert, RecordDelete, RecordUpsert
from lorebase_scribe.sources.mapping import MappedRow, Period, RowMapper
from lorebase_store.materialize.rows import ELEMENT_PROVENANCE_KEY

ProvenanceFactory = Callable[[MappedRow], ProvenanceEntry]


@dataclass(frozen=True)
class StoredRecord:
    """A Record on the branch: content hash and values."""

    object_hash: bytes
    values: Mapping[str, JsonValue]


@dataclass
class CurrentState:
    """What the Source branch holds for the target Kind and the mapped Relations."""

    records: Mapping[str, StoredRecord] = field(default_factory=dict)
    owned: frozenset[str] = frozenset()
    #: Relation id -> from key -> to keys.
    links: Mapping[str, Mapping[str, frozenset[str]]] = field(default_factory=dict)


@dataclass(frozen=True)
class Change:
    """A data change and the provenance entry it is attributed to (`None`: the commit's);
    `periods` holds the entries of the values that carry a validity period."""

    change: DataChange
    provenance: ProvenanceEntry | None = None
    periods: Mapping[str, ProvenanceEntry] = field(default_factory=dict)

    @property
    def entries(self) -> list[ProvenanceEntry]:
        """Every provenance entry the change names."""
        own = [] if self.provenance is None else [self.provenance]
        return [*own, *self.periods.values()]

    def attribution(self) -> str | dict[str, str] | None:
        """What the commit attributes the change to: the entry id, or a map from Attribute id
        to entry id when values carry periods (every value of the Record is named, so none
        falls back to the commit's own entry)."""
        if self.provenance is None:
            return None
        if not self.periods:
            return self.provenance.id
        named: dict[str, str] = {ELEMENT_PROVENANCE_KEY: self.provenance.id}
        if isinstance(self.change, RecordUpsert):
            named.update(dict.fromkeys(self.change.record.values, self.provenance.id))
        named.update({attribute: entry.id for attribute, entry in self.periods.items()})
        return named


@dataclass(frozen=True)
class ChangeGroup:
    key: str
    changes: tuple[Change, ...]


@dataclass
class Delta:
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0
    links_added: int = 0
    links_removed: int = 0
    groups: list[ChangeGroup] = field(default_factory=list)

    @property
    def change_count(self) -> int:
        return sum(len(group.changes) for group in self.groups)

    def counts(self) -> dict[str, int]:
        return {
            "added": len(self.added),
            "changed": len(self.changed),
            "removed": len(self.removed),
            "unchanged": self.unchanged,
            "links_added": self.links_added,
            "links_removed": self.links_removed,
        }


def content_hash(record: Record) -> bytes:
    """Object hash of a Record, as the commit pipeline stores it (design 6.1)."""
    body = normalize(record.content())
    assert isinstance(body, dict)
    return object_hash(ObjectType.RECORD, body)


def _link_changes(
    relation_id: str,
    key: str,
    desired: frozenset[str],
    existing: frozenset[str],
    provenance: ProvenanceEntry | None,
    delta: Delta,
) -> list[Change]:
    changes: list[Change] = []
    for to_key in sorted(existing - desired):
        changes.append(Change(LinkDelete(relation=relation_id, from_key=key, to_key=to_key)))
        delta.links_removed += 1
    for to_key in sorted(desired - existing):
        link = Link(relation=relation_id, from_key=key, to_key=to_key)
        changes.append(Change(LinkUpsert(link=link), provenance))
        delta.links_added += 1
    return changes


def _removal(mapper: RowMapper, key: str, current: CurrentState, delta: Delta) -> ChangeGroup:
    changes: list[Change] = []
    for relation_id in mapper.relations:
        existing = current.links.get(relation_id, {}).get(key, frozenset())
        changes.extend(_link_changes(relation_id, key, frozenset(), existing, None, delta))
    changes.append(Change(RecordDelete(kind=mapper.kind.id, key=key)))
    delta.removed.append(key)
    return ChangeGroup(key, tuple(changes))


def with_period(entry: ProvenanceEntry, period: Period) -> ProvenanceEntry:
    """A copy of `entry` carrying `period` (the caller gives it a fresh id)."""
    valid_from, valid_to = period
    return ProvenanceEntry.model_validate(
        {**entry.model_dump(mode="json"), "valid_from": valid_from, "valid_to": valid_to}
    )


def _period_entries(
    row: MappedRow,
    record: Record,
    stored: StoredRecord | None,
    provenance: ProvenanceFactory,
) -> dict[str, ProvenanceEntry]:
    """Entries of the values of `row` that carry a period and that the change writes."""
    found: dict[str, ProvenanceEntry] = {}
    for attribute_id, period in row.periods.items():
        value = record.values.get(attribute_id)
        if value is None or (stored is not None and stored.values.get(attribute_id) == value):
            continue
        found[attribute_id] = with_period(provenance(row), period)
    return found


def compute_delta(
    mapper: RowMapper,
    rows: Iterable[MappedRow],
    current: CurrentState,
    *,
    delete_missing: bool,
    provenance: ProvenanceFactory,
) -> Delta:
    """Changes turning the branch into what the rows say. Later rows of a key win."""
    latest: dict[str, MappedRow] = {}
    for row in rows:
        latest[row.key] = row
    delta = Delta()
    for key, row in latest.items():
        stored = current.records.get(key)
        if row.tombstone:
            if stored is not None:
                delta.groups.append(_removal(mapper, key, current, delta))
            continue
        record = mapper.record(row, None if stored is None else stored.values)
        entry: ProvenanceEntry | None = None
        changes: list[Change] = []
        if stored is None or content_hash(record) != stored.object_hash:
            entry = provenance(row)
            periods = _period_entries(row, record, stored, provenance)
            changes.append(Change(RecordUpsert(record=record), entry, periods))
            (delta.added if stored is None else delta.changed).append(key)
        else:
            delta.unchanged += 1
        for relation_id in mapper.relations:
            existing = current.links.get(relation_id, {}).get(key, frozenset())
            desired = row.links.get(relation_id, frozenset())
            if desired != existing and entry is None:
                entry = provenance(row)
            changes.extend(_link_changes(relation_id, key, desired, existing, entry, delta))
        if changes:
            delta.groups.append(ChangeGroup(key, tuple(changes)))
    if delete_missing:
        for key in sorted(current.owned - latest.keys()):
            if key in current.records:
                delta.groups.append(_removal(mapper, key, current, delta))
    return delta


def batches(groups: Iterable[ChangeGroup], size: int) -> list[list[ChangeGroup]]:
    """Groups packed into batches of about `size` changes; a group is never split."""
    out: list[list[ChangeGroup]] = []
    current: list[ChangeGroup] = []
    count = 0
    for group in groups:
        if current and count + len(group.changes) > size:
            out.append(current)
            current, count = [], 0
        current.append(group)
        count += len(group.changes)
    if current:
        out.append(current)
    return out
