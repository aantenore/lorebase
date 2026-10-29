"""Candidates to Proposal items (design 9.2 step 11).

- A mention resolved as `new` becomes a `record` item holding its key and the best value per
  Attribute found for it (defaults filled in). Its confidence is the lowest of the mention's and
  its values'; it records the required Attributes and required Relations still missing.
- Values of existing Records become `value` items, one per Record and Attribute (the most
  confident); values equal to the current one are dropped as unchanged.
- Links become `link` items, one per Relation and endpoints (the most confident); Links that
  exist already are dropped. A Link of a foreign-key backed Relation becomes a value of its
  Attribute.
- Items below `min_confidence` are dropped. An item is accepted at once when its confidence
  reaches `auto_accept_threshold`, its match is not ambiguous, and, for a new Record, nothing
  required is missing; a Link is accepted at once only when its endpoints exist or are accepted
  at once too. Items that set a value of a vault Attribute are never accepted at once: the
  extraction commit checks vault values against erased ones, so a reviewer decides (design
  13.5). A value item `hold` says raises a reconciliation contradiction (design 10.6,
  `hold_auto_accept`) is not accepted at once either.
- A value's validity period (a `valid_time` Attribute) goes with its evidence (`valid_from`,
  `valid_to`), from where the commit puts it on the provenance entry.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from lorebase_core.model import RelationBacking, new_ulid
from lorebase_core.types import EncodingError, encode_value
from lorebase_scribe.extraction.candidates import LinkCandidate, Resolved, ValueCandidate
from lorebase_scribe.extraction.ontology import Ontology
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_store.extraction import link_element, record_element
from lorebase_store.models.enums import ItemDecision
from lorebase_store.repositories.proposal_items import (
    LINK_ITEM,
    RECORD_ITEM,
    VALUE_ITEM,
    ItemRow,
)

ITEM_PREFIX = "pit"

RecordKey = tuple[str, str]


@dataclass(frozen=True)
class CurrentState:
    """What the target branch holds for the Records and Links the candidates name."""

    records: Mapping[RecordKey, Mapping[str, Any]]
    links: frozenset[tuple[str, str, str]]


@dataclass
class Assembled:
    items: list[ItemRow] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def count(self, name: str, n: int = 1) -> None:
        self.stats[name] = self.stats.get(name, 0) + n


def _best_values(
    values: Iterable[ValueCandidate],
) -> dict[RecordKey, dict[str, ValueCandidate]]:
    best: dict[RecordKey, dict[str, ValueCandidate]] = {}
    for value in values:
        if not value.subject.usable:
            continue
        element = (value.subject.kind_id, str(value.subject.key))
        per = best.setdefault(element, {})
        current = per.get(value.attribute_id)
        if current is None or value.confidence > current.confidence:
            per[value.attribute_id] = value
    return best


def _best_links(links: Iterable[LinkCandidate]) -> dict[tuple[str, str, str], LinkCandidate]:
    best: dict[tuple[str, str, str], LinkCandidate] = {}
    for link in links:
        if not (link.source.usable and link.target.usable):
            continue
        element = (link.relation_id, str(link.source.key), str(link.target.key))
        if element not in best or link.confidence > best[element].confidence:
            best[element] = link
    return best


def assemble_items(
    ontology: Ontology,
    mentions: Sequence[Resolved],
    values: Sequence[ValueCandidate],
    links: Sequence[LinkCandidate],
    current: CurrentState,
    *,
    job_id: str,
    principal: str,
    now: datetime,
    settings: ExtractionSettings,
    hold: Callable[[ValueCandidate], bool] | None = None,
) -> Assembled:
    """Items of an extraction Proposal, in order: Records, values, Links. `hold` names the
    value items that must stay pending whatever their confidence (module doc)."""
    result = Assembled()
    threshold = settings.auto_accept_threshold
    schema = ontology.schema
    new_records: dict[RecordKey, Resolved] = {}
    for mention in sorted(mentions, key=lambda m: (m.mention.start, m.kind_id)):
        if mention.resolution.status == "new" and mention.key is not None:
            new_records.setdefault((mention.kind_id, mention.key), mention)

    pending_values = list(values)
    best_links = _best_links(links)
    for (relation_id, from_key, to_key), link in list(best_links.items()):
        relation = schema.relations[relation_id]
        if relation.backing is RelationBacking.FOREIGN_KEY and relation.foreign_key_attribute:
            del best_links[(relation_id, from_key, to_key)]
            pending_values.append(
                ValueCandidate(
                    subject=link.source,
                    attribute_id=relation.foreign_key_attribute,
                    value=to_key,
                    evidence=link.evidence,
                    confidence=link.confidence,
                    tier=link.tier,
                    model=link.model,
                )
            )
    best_values = _best_values(pending_values)

    drafts: list[tuple[int, ItemRow]] = []
    auto_records: set[RecordKey] = set()

    def decide(confident: bool) -> tuple[ItemDecision, str | None, datetime | None]:
        if confident:
            return ItemDecision.ACCEPTED, principal, now
        return ItemDecision.PENDING, None, None

    for element, mention in new_records.items():
        kind_id, key = element
        view = ontology.kinds[kind_id]
        record_values: dict[str, Any] = {}
        evidence: dict[str, Any] = {}
        confidences = [mention.confidence]
        if view.key_attribute is not None:
            try:
                record_values[view.key_attribute.id] = encode_value(view.key_attribute.type, key)
            except EncodingError:
                result.count("invalid_key")
                continue
            evidence[view.key_attribute.id] = mention.mention.evidence.as_dict()
        for attribute_id, value in best_values.get(element, {}).items():
            record_values[attribute_id] = value.value
            evidence[attribute_id] = {**value.evidence_dict(), "model": value.model}
            confidences.append(value.confidence)
        for attribute in view.kind.attributes.values():
            if attribute.id not in record_values and attribute.default is not None:
                record_values[attribute.id] = attribute.default
        missing = sorted(
            a.api_name
            for a in view.kind.attributes.values()
            if a.required and not a.is_derived and a.id not in record_values
        )
        needed = [
            r
            for r in ontology.relations.values()
            if r.required_from and r.from_kind == kind_id and r.backing is RelationBacking.EXPLICIT
        ]
        provided = {
            relation_id
            for (relation_id, from_key, _to), link in best_links.items()
            if from_key == key
            and schema.relations[relation_id].from_kind == kind_id
            and link.confidence >= threshold
            and link.target.resolution.status == "matched"
        }
        missing_links = sorted(r.api_name for r in needed if r.id not in provided)
        confidence = min(confidences)
        if confidence < settings.min_confidence:
            result.count("below_min_confidence")
            continue
        vaulted = any(
            view.kind.attributes[a].in_vault
            for a, v in record_values.items()
            if v is not None and a in view.kind.attributes
        )
        confident = confidence >= threshold and not missing and not missing_links and not vaulted
        if confident:
            auto_records.add(element)
        decision, by, at = decide(confident)
        partition, element_key = record_element(kind_id, key)
        drafts.append(
            (
                mention.mention.start,
                ItemRow(
                    item_id=f"{ITEM_PREFIX}_{new_ulid()}",
                    item_type=RECORD_ITEM,
                    seq=0,
                    subject={
                        "kind": kind_id,
                        "key": key,
                        "values": record_values,
                        "evidence": evidence,
                    },
                    partition=partition,
                    element_key=element_key,
                    evidence=mention.mention.evidence.as_dict(),
                    confidence=round(confidence, 4),
                    decision=decision,
                    decided_by=by,
                    decided_at=at,
                    job_id=job_id,
                    match={
                        **mention.resolution.as_dict(),
                        "complete": not missing and not missing_links,
                        "missing": missing,
                        "missing_links": missing_links,
                    },
                    tier=mention.mention.tier,
                    model=mention.mention.extractor,
                ),
            )
        )

    for element, per_attribute in best_values.items():
        if element in new_records:
            continue
        stored = current.records.get(element)
        if stored is None:
            result.count("unknown_record", len(per_attribute))
            continue
        for attribute_id, value in per_attribute.items():
            if stored.get(attribute_id) == value.value:
                result.count("unchanged")
                continue
            if value.confidence < settings.min_confidence:
                result.count("below_min_confidence")
                continue
            resolution = value.subject.resolution
            target = schema.kinds[element[0]].attributes.get(attribute_id)
            confident = (
                value.confidence >= threshold
                and resolution.status == "matched"
                and not (target is not None and target.in_vault)
            )
            if confident and hold is not None and hold(value):
                result.count("held_by_reconciliation")
                confident = False
            decision, by, at = decide(confident)
            partition, element_key = record_element(*element)
            drafts.append(
                (
                    value.evidence.start,
                    ItemRow(
                        item_id=f"{ITEM_PREFIX}_{new_ulid()}",
                        item_type=VALUE_ITEM,
                        seq=0,
                        subject={
                            "kind": element[0],
                            "key": element[1],
                            "attribute": attribute_id,
                            "value": value.value,
                            "current": stored.get(attribute_id),
                        },
                        partition=partition,
                        element_key=element_key,
                        attribute_id=attribute_id,
                        evidence=value.evidence_dict(),
                        confidence=round(value.confidence, 4),
                        decision=decision,
                        decided_by=by,
                        decided_at=at,
                        job_id=job_id,
                        match=resolution.as_dict(),
                        tier=value.tier,
                        model=value.model,
                    ),
                )
            )

    for (relation_id, from_key, to_key), link in best_links.items():
        if (relation_id, from_key, to_key) in current.links:
            result.count("unchanged")
            continue
        if link.confidence < settings.min_confidence:
            result.count("below_min_confidence")
            continue

        def ready(side: Resolved) -> bool:
            if side.resolution.status == "matched":
                return True
            return (side.kind_id, str(side.key)) in auto_records

        missing_attributes = sorted(
            a.api_name
            for a in schema.relations[relation_id].attributes.values()
            if a.required and not a.is_derived and a.id not in link.values
        )
        confident = (
            link.confidence >= threshold
            and ready(link.source)
            and ready(link.target)
            and not missing_attributes
        )
        decision, by, at = decide(confident)
        partition, element_key = link_element(relation_id, from_key, to_key)
        drafts.append(
            (
                link.evidence.start,
                ItemRow(
                    item_id=f"{ITEM_PREFIX}_{new_ulid()}",
                    item_type=LINK_ITEM,
                    seq=0,
                    subject={
                        "relation": relation_id,
                        "from": from_key,
                        "to": to_key,
                        "values": dict(link.values),
                    },
                    partition=partition,
                    element_key=element_key,
                    evidence=link.evidence.as_dict(),
                    confidence=round(link.confidence, 4),
                    decision=decision,
                    decided_by=by,
                    decided_at=at,
                    job_id=job_id,
                    match={
                        "from": link.source.resolution.as_dict(),
                        "to": link.target.resolution.as_dict(),
                        "missing": missing_attributes,
                    },
                    tier=link.tier,
                    model=link.model,
                ),
            )
        )

    order = {RECORD_ITEM: 0, VALUE_ITEM: 1, LINK_ITEM: 2}
    drafts.sort(key=lambda d: (order[d[1].item_type], d[0], d[1].partition, d[1].element_key))
    if len(drafts) > settings.max_items_per_job:
        result.count("over_item_limit", len(drafts) - settings.max_items_per_job)
        drafts = drafts[: settings.max_items_per_job]
    result.items = [replace(row, seq=index) for index, (_start, row) in enumerate(drafts)]
    return result
