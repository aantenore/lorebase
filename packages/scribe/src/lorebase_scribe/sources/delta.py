"""The delta of a Source run (design 9.1 steps 2-4, "Corrections that stick").

Pure: the current state of the Source branch and the Source's last-written state are passed
in. Per Attribute the fresh value `F` is compared with the value the Source last wrote `W`,
not with the current one `C`: only upstream changes are written, so a value someone else set
stays until upstream changes it. An upstream change over a value someone else set (`C` is
neither `W` nor `F`, and its Lineage is not this Source) is a **local edit**: `F` is written
and, unless the Source's `local_edits` is `source_wins`, the Attribute is listed in
`StateDraft.preserve` so the Proposal's merge base keeps `W` and the merge raises a Conflict.
An element or Attribute without last-written state is seeded: `W := C` when this Source set
`C`, otherwise compare-only (`F = C` seeds, `F != C` is a local edit).

A row is `added` when its key is not on the branch, `changed` when its Record changes, and
otherwise unchanged (Lineage is not content). Links of the mapped Relations follow the rows,
against the Links the Source last wrote. `removed` keys are the keys of the last-written
state (and, before any state, the owned keys) the run did not see, when deletions apply, plus
the keys of tombstone rows that exist; a removal of a Record holding values someone else set
is a local edit too. The changes of one key are kept together in a `ChangeGroup`, so a batch
boundary never splits a Record from its Links. `states` holds the last-written state the run
advances to when its Proposal merges.

A value whose row maps a validity period (`MappedRow.periods`) and that the change writes (new
or different from the stored one) gets a provenance entry of its own carrying the period; the
other values of the change share the row's entry (design 10.6).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import JsonValue

from lorebase_core.canonical import ObjectType, canonical_bytes, normalize, object_hash
from lorebase_core.model import Link, LocalEdits, ProvenanceEntry, Record
from lorebase_core.validate import DataChange, LinkDelete, LinkUpsert, RecordDelete, RecordUpsert
from lorebase_scribe.sources.mapping import MappedRow, Period, RowMapper
from lorebase_store.materialize.rows import ELEMENT_PROVENANCE_KEY
from lorebase_store.repositories.source_written import WrittenRow

ProvenanceFactory = Callable[[MappedRow], ProvenanceEntry]


@dataclass(frozen=True)
class StoredRecord:
    """A Record on the branch: content hash and values."""

    object_hash: bytes
    values: Mapping[str, JsonValue]


@dataclass(frozen=True)
class Setter:
    """Who set a current value that this Source did not write: the origin type and actor of
    the provenance entry of its last change."""

    origin_type: str
    actor: str


@dataclass
class CurrentState:
    """What the Source branch holds for the target Kind and the mapped Relations."""

    records: Mapping[str, StoredRecord] = field(default_factory=dict)
    owned: frozenset[str] = frozenset()
    #: Relation id -> from key -> to keys.
    links: Mapping[str, Mapping[str, frozenset[str]]] = field(default_factory=dict)
    #: `(key, attribute)` -> who set the value, for the values whose Lineage is not this
    #: Source; values missing here were set by this Source.
    foreign: Mapping[tuple[str, str], Setter] = field(default_factory=dict)
    #: Relation id -> `(from, to)` of the Links whose Lineage is this Source; `None`: every
    #: Link is.
    owned_links: Mapping[str, frozenset[tuple[str, str]]] | None = None


@dataclass(frozen=True)
class WrittenState:
    """What the Source last wrote: its last-written state (the open Proposal's, where it
    carries one, else the merged one) by key, Links by Relation id and from key, and the
    Attributes the open Proposal already preserves."""

    records: Mapping[str, WrittenRow] = field(default_factory=dict)
    links: Mapping[str, Mapping[str, WrittenRow]] = field(default_factory=dict)
    preserve: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


class Outcome(StrEnum):
    """What became of a local edit (design 9.1)."""

    CONFLICT = "conflict"
    KEPT = "kept"
    OVERWRITTEN = "overwritten"
    DELETED = "deleted"


#: Origin types a `keep` Source ranks above itself: a person's or a decision's values
#: (the ranking `[principal:*, operation, merge_resolution, reconciliation, source:{id}]`).
KEPT_ORIGINS = frozenset({"operation", "merge_resolution", "reconciliation"})


@dataclass(frozen=True)
class EditRules:
    """How local edits are handled: the Source's `local_edits`, and the Attributes always
    overwritten (those whose `authority` is the Source under `auto_with_authority`)."""

    mode: LocalEdits = LocalEdits.REVIEW
    overwrite: frozenset[str] = frozenset()

    def mode_for(self, attribute: str | None) -> LocalEdits:
        if attribute is not None and attribute in self.overwrite:
            return LocalEdits.SOURCE_WINS
        return self.mode

    def outcome(self, attribute: str | None, setter: Setter | None) -> Outcome:
        """The outcome of a local edit of `attribute` (None: the whole Record)."""
        mode = self.mode_for(attribute)
        if mode is LocalEdits.SOURCE_WINS:
            return Outcome.OVERWRITTEN
        if mode is LocalEdits.REVIEW:
            return Outcome.CONFLICT
        if attribute is None:
            return Outcome.KEPT
        if setter is None:
            return Outcome.CONFLICT  # who set it is unknown: the ranking cannot decide
        return Outcome.KEPT if setter.origin_type in KEPT_ORIGINS else Outcome.OVERWRITTEN


@dataclass(frozen=True)
class LocalEdit:
    """A local edit met by the run: `attribute` None for a whole Record (a removal of a
    Record holding someone else's values, or an upstream change of a Record someone else
    deleted)."""

    key: str
    attribute: str | None
    setter: Setter | None
    outcome: Outcome

    @property
    def preserved(self) -> bool:
        """Whether the merge raises a Conflict for it (its base is preserved)."""
        return self.outcome is not Outcome.OVERWRITTEN and self.outcome is not Outcome.DELETED


@dataclass(frozen=True)
class StateDraft:
    """The last-written state of an element after the run: value hashes by Attribute (None:
    absent; Links: one entry per target key), the Revisions holding the values that did not
    change (Attribute id -> hex; the others are on the run's head), whether the Source
    deleted the element, and the Attributes whose merge base keeps the last-written value."""

    element: str
    key: str
    written: Mapping[str, str | None]
    kept: Mapping[str, str] = field(default_factory=dict)
    deleted: bool = False
    preserve: tuple[str, ...] = ()

    def row(self, revision: bytes, run_id: str) -> WrittenRow:
        """The state row with the run's head `revision`."""
        head = revision.hex()
        return WrittenRow(
            element=self.element,
            key=self.key,
            written=dict(self.written),
            revision_id=revision,
            run_id=run_id,
            value_revisions={a: r for a, r in self.kept.items() if r != head},
            deleted=self.deleted,
            preserve=self.preserve,
        )


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
    local_edits: list[LocalEdit] = field(default_factory=list)
    states: list[StateDraft] = field(default_factory=list)
    #: Keys seen without last-written state (seeded by this run).
    seeded: int = 0

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

    def local_edit_counts(self) -> dict[str, int]:
        """Local edits per outcome."""
        found: dict[str, int] = {}
        for edit in self.local_edits:
            found[edit.outcome.value] = found.get(edit.outcome.value, 0) + 1
        return found


def content_hash(record: Record) -> bytes:
    """Object hash of a Record, as the commit pipeline stores it (design 6.1)."""
    body = normalize(record.content())
    assert isinstance(body, dict)
    return object_hash(ObjectType.RECORD, body)


def value_digest(value: JsonValue) -> str | None:
    """Hex SHA-256 of the canonical JSON of a value (None: absent)."""
    if value is None:
        return None
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _link_changes(
    relation_id: str,
    key: str,
    adds: Iterable[str],
    removes: Iterable[str],
    provenance: ProvenanceEntry | None,
    delta: Delta,
) -> list[Change]:
    changes: list[Change] = []
    for to_key in sorted(removes):
        changes.append(Change(LinkDelete(relation=relation_id, from_key=key, to_key=to_key)))
        delta.links_removed += 1
    for to_key in sorted(adds):
        link = Link(relation=relation_id, from_key=key, to_key=to_key)
        changes.append(Change(LinkUpsert(link=link), provenance))
        delta.links_added += 1
    return changes


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


class _Run:
    """One computation of `compute_delta`."""

    def __init__(
        self,
        mapper: RowMapper,
        current: CurrentState,
        written: WrittenState,
        rules: EditRules,
        provenance: ProvenanceFactory,
    ) -> None:
        self.mapper = mapper
        self.current = current
        self.written = written
        self.rules = rules
        self.provenance = provenance
        self.delta = Delta()

    # Records ----------------------------------------------------------------------------

    def fresh(self, row: MappedRow) -> dict[str, str | None]:
        """Hashes of the fresh values of every Attribute the Source owns."""
        values = row.values
        return {a: value_digest(values.get(a)) for a in sorted(self.mapper.owned_attributes)}

    def edit(self, key: str, attribute: str | None, setter: Setter | None) -> LocalEdit:
        found = LocalEdit(key, attribute, setter, self.rules.outcome(attribute, setter))
        self.delta.local_edits.append(found)
        return found

    def upsert(self, row: MappedRow) -> None:
        key = row.key
        delta, current = self.delta, self.current
        stored = current.records.get(key)
        last = self.written.records.get(key)
        if last is None:
            delta.seeded += 1
        fresh = self.fresh(row)
        preserve: set[str] = set(self.written.preserve.get(key, ()))
        kept: dict[str, str] = {}
        if stored is None:
            if last is not None and not last.deleted:
                # Someone else deleted the Record the Source wrote.
                if _present(fresh) == _present(last.written):
                    delta.unchanged += 1
                    return
                if self.edit(key, None, None).preserved:
                    preserve.update(a for a, h in last.written.items() if h is not None)
            record = self.mapper.record(row, None)
        else:
            record = self.updated(row, stored, last, fresh, preserve, kept)
        entry: ProvenanceEntry | None = None
        changes: list[Change] = []
        if stored is None or content_hash(record) != stored.object_hash:
            entry = self.provenance(row)
            periods = _period_entries(row, record, stored, self.provenance)
            changes.append(Change(RecordUpsert(record=record), entry, periods))
            (delta.added if stored is None else delta.changed).append(key)
        else:
            delta.unchanged += 1
        for relation_id in self.mapper.relations:
            desired = row.links.get(relation_id, frozenset())
            adds, removes = self.link_changes(relation_id, key, desired)
            if (adds or removes) and entry is None:
                entry = self.provenance(row)
            changes.extend(_link_changes(relation_id, key, adds, removes, entry, delta))
            self.state(
                StateDraft(relation_id, key, {to: value_digest(to) for to in sorted(desired)}),
                self.written.links.get(relation_id, {}).get(key),
            )
        if changes:
            delta.groups.append(ChangeGroup(key, tuple(changes)))
        self.state(
            StateDraft(self.mapper.kind.id, key, fresh, kept, preserve=tuple(sorted(preserve))),
            last,
        )

    def state(self, draft: StateDraft, last: WrittenRow | None) -> None:
        """Keep `draft` unless it says what `last` already says (most rows of most runs)."""
        unchanged = (
            last is not None
            and not draft.preserve
            and not last.preserve
            and draft.deleted == last.deleted
            and dict(draft.written) == dict(last.written)
        )
        if not unchanged:
            self.delta.states.append(draft)

    def updated(
        self,
        row: MappedRow,
        stored: StoredRecord,
        last: WrittenRow | None,
        fresh: Mapping[str, str | None],
        preserve: set[str],
        kept: dict[str, str],
    ) -> Record:
        """The current Record with the upstream changes of `row` written (module doc)."""
        key = row.key
        values = dict(stored.values)
        for attribute, f in fresh.items():
            c = value_digest(stored.values.get(attribute))
            setter = self.current.foreign.get((key, attribute))
            known = last is not None and (last.deleted or attribute in last.written)
            if not known:
                # Seeding: `W := C` when this Source set `C`, else compare-only.
                if f == c:
                    continue
                foreign = stored.values.get(attribute) is not None and setter is not None
                if foreign and self.edit(key, attribute, setter).preserved:
                    preserve.add(attribute)
                _write(values, attribute, row.values.get(attribute))
                continue
            assert last is not None
            w = None if last.deleted else last.written.get(attribute)
            if f == w:
                revision = None if last.deleted else last.revision_of(attribute)
                if revision is not None:
                    kept[attribute] = revision.hex()
                continue
            if f == c:
                continue  # upstream caught up with the current value
            edited = c != w and (setter is not None or c is None)
            if edited and self.edit(key, attribute, setter).preserved:
                preserve.add(attribute)
            _write(values, attribute, row.values.get(attribute))
        return Record(kind=self.mapper.kind.id, key=key, values=values)

    # Links ------------------------------------------------------------------------------

    def link_changes(
        self, relation_id: str, key: str, desired: frozenset[str]
    ) -> tuple[set[str], set[str]]:
        """Links to add and remove: the upstream changes since the Source last wrote them;
        before any state, the differences with the branch (removing only the Source's own
        Links)."""
        existing = self.current.links.get(relation_id, {}).get(key, frozenset())
        last = self.written.links.get(relation_id, {}).get(key)
        if last is None:
            owned_links = self.current.owned_links
            removes = {
                to
                for to in existing - desired
                if owned_links is None or (key, to) in owned_links.get(relation_id, frozenset())
            }
            return set(desired - existing), removes
        previous = set(last.written)
        return set(desired - previous - existing), (previous - desired) & existing

    # Removals ---------------------------------------------------------------------------

    def remove(self, key: str) -> None:
        delta, current = self.delta, self.current
        stored = current.records[key]
        last = self.written.records.get(key)
        others = [a for a in sorted(stored.values) if self.foreign(key, a, stored, last)]
        preserve: tuple[str, ...] = ()
        if others:
            edit = self.edit(key, None, current.foreign.get((key, others[0])))
            if edit.outcome is Outcome.OVERWRITTEN:
                delta.local_edits[-1] = LocalEdit(key, None, edit.setter, Outcome.DELETED)
            elif edit.preserved:
                preserve = tuple(others)
        changes: list[Change] = []
        for relation_id in self.mapper.relations:
            existing = current.links.get(relation_id, {}).get(key, frozenset())
            changes.extend(_link_changes(relation_id, key, (), existing, None, delta))
            delta.states.append(StateDraft(relation_id, key, {}, deleted=True))
        changes.append(Change(RecordDelete(kind=self.mapper.kind.id, key=key)))
        delta.removed.append(key)
        delta.groups.append(ChangeGroup(key, tuple(changes)))
        delta.states.append(
            StateDraft(self.mapper.kind.id, key, {}, deleted=True, preserve=preserve)
        )

    def foreign(
        self, key: str, attribute: str, stored: StoredRecord, last: WrittenRow | None
    ) -> bool:
        """Whether someone other than this Source set the current value of `attribute`."""
        if (key, attribute) not in self.current.foreign:
            return False
        if last is None or last.deleted or attribute not in last.written:
            return True
        return value_digest(stored.values.get(attribute)) != last.written[attribute]


def _present(written: Mapping[str, str | None]) -> dict[str, str]:
    return {a: h for a, h in written.items() if h is not None}


def _write(values: dict[str, JsonValue], attribute: str, value: JsonValue) -> None:
    if value is None:
        values.pop(attribute, None)
    else:
        values[attribute] = value


def compute_delta(
    mapper: RowMapper,
    rows: Iterable[MappedRow],
    current: CurrentState,
    written: WrittenState | None = None,
    *,
    delete_missing: bool,
    provenance: ProvenanceFactory,
    rules: EditRules | None = None,
) -> Delta:
    """Changes turning the branch into what the rows say, upstream changes only (module
    doc). Later rows of a key win."""
    run = _Run(mapper, current, written or WrittenState(), rules or EditRules(), provenance)
    latest: dict[str, MappedRow] = {}
    for row in rows:
        latest[row.key] = row
    for key, row in latest.items():
        if row.tombstone:
            if key in current.records:
                run.remove(key)
            continue
        run.upsert(row)
    if delete_missing:
        state = run.written.records
        candidates = {k for k, w in state.items() if not w.deleted} | (current.owned - state.keys())
        for key in sorted(candidates - latest.keys()):
            if key in current.records:
                run.remove(key)
    return run.delta


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
