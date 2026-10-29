"""Materialize the accepted items of an extraction Proposal on its branch (design 9.2 step 12).

The extraction branch holds, for every element an item touches, the target branch's current
content with the accepted items applied, and nothing else changes. Each call compares that
desired state with the branch head and commits the difference as one Revision, so accepting
an item adds its change and rejecting an accepted one reverts it. Elements no item touches keep
whatever the branch holds. Calling it again without new decisions commits nothing.

Rules:

- a `record` item creates the Record (or sets its values on a Record that exists by now);
- a `value` item sets one Attribute of an existing Record; it is skipped when the Record does
  not exist;
- a `link` item adds the Link when both endpoints exist (or are created by accepted items);
  on a Relation that allows one Link per `from` (or `to`) Record, the Links it replaces are
  removed; Links of foreign-key backed Relations are skipped (they derive from an Attribute);
- every value set by an item is attributed to an `extraction` provenance entry with the job,
  document, evidence offsets, model and prompt version, and the validity period its evidence
  states (`valid_from`, `valid_to`; values of `valid_time` Attributes, design 10.6).

Validation is the commit pipeline's: an accepted Record that misses a required value fails the
commit with `ValidationFailedError` and nothing is written.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import (
    ExtractionOrigin,
    IdPrefix,
    Link,
    ProvenanceEntry,
    Record,
    RelationBacking,
    Schema,
    new_id,
)
from lorebase_core.revision import Author, Origin
from lorebase_core.types.encoding import format_timestamp
from lorebase_core.validate import DataChange, LinkDelete, LinkUpsert, RecordDelete, RecordUpsert
from lorebase_store.commit import Change, commit
from lorebase_store.extraction.items import effective_subject
from lorebase_store.models.enums import ItemDecision
from lorebase_store.repositories.materialized import MaterializedRepository
from lorebase_store.repositories.proposal_items import LINK_ITEM, RECORD_ITEM, VALUE_ITEM, ItemRow
from lorebase_store.runtime import StoreRuntime

EXTRACTION_ORIGIN = "extraction"
RULES_MODEL = "rules"
"""Model id recorded for items produced without a model (tier 0)."""

RecordId = tuple[str, str]
LinkId = tuple[str, str, str]
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class MaterializeContext:
    """Where and as whom to materialize."""

    domain_id: str
    branch: str
    target: str
    job_id: str
    prompt_version: str
    actor: str
    runtime: StoreRuntime
    clock: Clock


@dataclass
class Materialized:
    """Outcome: the Revision committed (none when the branch already matched), the number of
    changes, and the accepted items that could not be applied with the reason."""

    revision: bytes | None = None
    changes: int = 0
    applied: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "revision": None if self.revision is None else self.revision.hex(),
            "changes": self.changes,
            "applied": len(self.applied),
            "skipped": dict(self.skipped),
        }


@dataclass
class _Plan:
    records: dict[RecordId, dict[str, Any] | None] = field(default_factory=dict)
    links: dict[LinkId, dict[str, Any] | None] = field(default_factory=dict)
    record_sources: dict[RecordId, dict[str, str]] = field(default_factory=dict)
    link_sources: dict[LinkId, str] = field(default_factory=dict)


class _Provenance:
    """One provenance entry per distinct evidence span of an item."""

    def __init__(self, context: MaterializeContext) -> None:
        self._context = context
        self._entries: dict[tuple[str, int, int, str, Any, Any], ProvenanceEntry] = {}

    def entry(self, item: ItemRow, evidence: Mapping[str, Any] | None) -> str:
        found = evidence or item.evidence or {}
        start = int(found.get("start") or 0)
        end = max(int(found.get("end") or 0), start)
        document = str(found.get("document_id") or "")
        model = str(found.get("model") or item.model or RULES_MODEL)
        valid_from, valid_to = found.get("valid_from"), found.get("valid_to")
        key = (document, start, end, model, valid_from, valid_to)
        if key not in self._entries:
            self._entries[key] = ProvenanceEntry(
                id=new_id(IdPrefix.PROVENANCE),
                origin=ExtractionOrigin(
                    job_id=item.job_id or self._context.job_id,
                    document_id=document,
                    span_start=start,
                    span_end=end,
                    model_id=model,
                    prompt_version=self._context.prompt_version,
                ),
                actor=self._context.actor,
                at=format_timestamp(self._context.clock()),
                valid_from=valid_from,
                valid_to=valid_to,
            )
        return self._entries[key].id

    def used(self, ids: set[str]) -> list[ProvenanceEntry]:
        return [entry for entry in self._entries.values() if entry.id in ids]


async def materialize_items(
    session: AsyncSession,
    context: MaterializeContext,
    items: Sequence[ItemRow],
    schema: Schema,
    *,
    message: str,
) -> Materialized:
    """Make the extraction branch hold the accepted `items` (see the module docstring)."""
    writes = context.runtime.settings.writes
    target = MaterializedRepository(session, context.domain_id, context.target, writes=writes)
    branch = MaterializedRepository(session, context.domain_id, context.branch, writes=writes)
    ordered = sorted(items, key=lambda item: (item.seq, item.item_id))
    subjects = {item.item_id: effective_subject(item) for item in ordered}

    record_keys: dict[str, set[str]] = defaultdict(set)
    link_pairs: dict[str, set[tuple[str, str]]] = defaultdict(set)
    endpoints: dict[str, set[str]] = defaultdict(set)
    for item in ordered:
        for subject in (item.subject, subjects[item.item_id]):
            if item.item_type == LINK_ITEM:
                relation = schema.relations.get(str(subject["relation"]))
                if relation is None:
                    continue
                pair = (str(subject["from"]), str(subject["to"]))
                link_pairs[relation.id].add(pair)
                endpoints[relation.from_kind].add(pair[0])
                endpoints[relation.to_kind].add(pair[1])
            else:
                record_keys[str(subject["kind"])].add(str(subject["key"]))
    await _replaced_links(target, schema, ordered, subjects, link_pairs)

    plan = _Plan()
    current_records: dict[RecordId, dict[str, Any]] = {}
    for kind_id, keys in record_keys.items():
        on_target = await target.records(kind_id, keys)
        on_branch = await branch.records(kind_id, keys)
        for key in keys:
            row = on_target.get(key)
            plan.records[(kind_id, key)] = None if row is None else dict(row.values)
            if key in on_branch:
                current_records[(kind_id, key)] = dict(on_branch[key].values)
    current_links: dict[LinkId, dict[str, Any]] = {}
    for relation_id, pairs in link_pairs.items():
        on_target_links = await target.links(relation_id, pairs)
        on_branch_links = await branch.links(relation_id, pairs)
        for pair in pairs:
            link = on_target_links.get(pair)
            plan.links[(relation_id, *pair)] = None if link is None else dict(link.values)
            if pair in on_branch_links:
                current_links[(relation_id, *pair)] = dict(on_branch_links[pair].values)
    existing: dict[str, set[str]] = {}
    for kind_id, keys in endpoints.items():
        existing[kind_id] = set(await branch.records(kind_id, keys))

    provenance = _Provenance(context)
    outcome = Materialized()
    accepted = [item for item in ordered if item.decision is ItemDecision.ACCEPTED]
    for item in accepted:
        if item.item_type != LINK_ITEM:
            _apply_record_item(item, subjects[item.item_id], plan, provenance, outcome)
    for item in accepted:
        if item.item_type == LINK_ITEM:
            _apply_link_item(
                item, subjects[item.item_id], schema, plan, existing, provenance, outcome
            )

    changes = _diff(plan, current_records, current_links)
    outcome.changes = len(changes)
    if changes:
        used = {
            value
            for change in changes
            for value in (
                change.provenance.values()
                if isinstance(change.provenance, Mapping)
                else [change.provenance]
            )
            if value is not None
        }
        result = await commit(
            session,
            context.domain_id,
            context.branch,
            changes,
            author=Author(principal=context.actor),
            message=message,
            origin=Origin(type=EXTRACTION_ORIGIN, ref=context.job_id),
            provenance=provenance.used(used),
            runtime=context.runtime,
            clock=context.clock,
        )
        if not result.no_op:
            outcome.revision = result.revision_id
    return outcome


async def _replaced_links(
    target: MaterializedRepository,
    schema: Schema,
    items: Sequence[ItemRow],
    subjects: Mapping[str, Mapping[str, Any]],
    link_pairs: dict[str, set[tuple[str, str]]],
) -> None:
    """Add the target's Links that a Link item would replace on single-valued Relations."""
    for item in items:
        if item.item_type != LINK_ITEM:
            continue
        subject = subjects[item.item_id]
        relation = schema.relations.get(str(subject["relation"]))
        if relation is None:
            continue
        from_key, to_key = str(subject["from"]), str(subject["to"])
        if relation.single_per_from:
            for _near, far, _values in await target.linked_keys(
                relation.id, near="from_key", keys=[from_key]
            ):
                link_pairs[relation.id].add((from_key, far))
        if relation.single_per_to:
            for _near, far, _values in await target.linked_keys(
                relation.id, near="to_key", keys=[to_key]
            ):
                link_pairs[relation.id].add((far, to_key))


def _apply_record_item(
    item: ItemRow,
    subject: Mapping[str, Any],
    plan: _Plan,
    provenance: _Provenance,
    outcome: Materialized,
) -> None:
    element = (str(subject["kind"]), str(subject["key"]))
    desired = plan.records.get(element)
    sources = plan.record_sources.setdefault(element, {})
    if item.item_type == RECORD_ITEM:
        values = dict(subject.get("values") or {})
        evidence = subject.get("evidence") or {}
        desired = {**(desired or {}), **values}
        for attribute_id in values:
            sources[attribute_id] = provenance.entry(item, evidence.get(attribute_id))
    elif item.item_type == VALUE_ITEM:
        if desired is None:
            outcome.skipped[item.item_id] = "the Record does not exist"
            return
        attribute_id = str(subject["attribute"])
        desired = {**desired, attribute_id: subject.get("value")}
        sources[attribute_id] = provenance.entry(item, None)
    plan.records[element] = {k: v for k, v in (desired or {}).items() if v is not None}
    outcome.applied.append(item.item_id)


def _apply_link_item(
    item: ItemRow,
    subject: Mapping[str, Any],
    schema: Schema,
    plan: _Plan,
    existing: Mapping[str, set[str]],
    provenance: _Provenance,
    outcome: Materialized,
) -> None:
    relation = schema.relations.get(str(subject["relation"]))
    if relation is None:
        outcome.skipped[item.item_id] = "the Relation no longer exists"
        return
    if relation.backing is RelationBacking.FOREIGN_KEY:
        outcome.skipped[item.item_id] = "Links of this Relation derive from an Attribute"
        return
    from_key, to_key = str(subject["from"]), str(subject["to"])

    def present(kind_id: str, key: str) -> bool:
        if (kind_id, key) in plan.records:
            return plan.records[(kind_id, key)] is not None
        return key in existing.get(kind_id, set())

    if not (present(relation.from_kind, from_key) and present(relation.to_kind, to_key)):
        outcome.skipped[item.item_id] = "an endpoint Record does not exist"
        return
    element = (relation.id, from_key, to_key)
    for other in list(plan.links):
        if other[0] != relation.id or other == element:
            continue
        if (relation.single_per_from and other[1] == from_key) or (
            relation.single_per_to and other[2] == to_key
        ):
            plan.links[other] = None
    plan.links[element] = {**(plan.links.get(element) or {}), **(subject.get("values") or {})}
    plan.link_sources[element] = provenance.entry(item, None)
    outcome.applied.append(item.item_id)


def _diff(
    plan: _Plan,
    current_records: Mapping[RecordId, dict[str, Any]],
    current_links: Mapping[LinkId, dict[str, Any]],
) -> list[Change]:
    removals: list[Change] = []
    upserts: list[Change] = []
    for (relation_id, from_key, to_key), values in sorted(
        plan.links.items(), key=lambda entry: entry[0]
    ):
        current = current_links.get((relation_id, from_key, to_key))
        if values is None and current is not None:
            removals.append(
                Change(LinkDelete(relation=relation_id, from_key=from_key, to_key=to_key))
            )
    for (kind_id, key), values in sorted(plan.records.items(), key=lambda entry: entry[0]):
        current = current_records.get((kind_id, key))
        if values is None:
            if current is not None:
                removals.append(Change(RecordDelete(kind=kind_id, key=key)))
        elif current != values:
            sources = plan.record_sources.get((kind_id, key)) or {}
            change: DataChange = RecordUpsert(record=Record(kind=kind_id, key=key, values=values))
            upserts.append(Change(change, {a: p for a, p in sources.items() if a in values}))
    for (relation_id, from_key, to_key), values in sorted(
        plan.links.items(), key=lambda entry: entry[0]
    ):
        current = current_links.get((relation_id, from_key, to_key))
        if values is not None and current != values:
            link = Link(relation=relation_id, from_key=from_key, to_key=to_key, values=values)
            upserts.append(
                Change(
                    LinkUpsert(link=link), plan.link_sources.get((relation_id, from_key, to_key))
                )
            )
    return removals + upserts
