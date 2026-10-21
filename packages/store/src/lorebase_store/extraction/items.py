"""Extraction items as data: element identity, reviewer edits and the Proposal summary.

Shared by Scribe, which writes the items of a new extraction Proposal, and Keeper, which
records reviewer decisions; neither needs a model for this.

Reviewer edits, by item type (Attributes named by id or api name, values in API form):

- `record`: `{"values": {attribute: value}}`, merged over the proposed values;
- `value`: `{"value": value, "key": key}`, the value, or the Record it belongs to (the
  ambiguous match picker);
- `link`: `{"from": key, "to": key, "values": {attribute: value}}`.

`normalize_edits` validates them against the schema and returns them with Attribute ids and
canonical values, the form stored in `proposal_items.edits` and applied by
`effective_subject`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from lorebase_core.model import Attribute, Schema
from lorebase_core.tree import kind_partition, link_key, relation_partition
from lorebase_core.types import EncodingError, encode_value, normalize_key
from lorebase_store.models.enums import ItemDecision
from lorebase_store.repositories.proposal_items import (
    LINK_ITEM,
    RECORD_ITEM,
    VALUE_ITEM,
    ItemRow,
)


class ItemEditError(ValueError):
    """Reviewer edits that do not fit the item or the schema."""

    def __init__(self, item_id: str, message: str) -> None:
        super().__init__(f"item {item_id}: {message}")
        self.item_id = item_id
        self.detail = message


def record_element(kind_id: str, key: str) -> tuple[str, str]:
    """`(partition, element key)` of a Record."""
    return kind_partition(kind_id), key


def link_element(relation_id: str, from_key: str, to_key: str) -> tuple[str, str]:
    """`(partition, element key)` of a Link."""
    return relation_partition(relation_id), link_key(from_key, to_key)


def _attributes(schema: Schema, item: ItemRow) -> Mapping[str, Attribute]:
    subject = item.subject
    if item.item_type == LINK_ITEM:
        relation = schema.relations.get(str(subject.get("relation")))
        if relation is None:
            raise ItemEditError(item.item_id, "the Relation no longer exists")
        return relation.attributes
    kind = schema.kinds.get(str(subject.get("kind")))
    if kind is None:
        raise ItemEditError(item.item_id, "the Kind no longer exists")
    return kind.attributes


def _encode(item_id: str, attribute: Attribute, raw: Any) -> Any:
    if attribute.is_derived:
        raise ItemEditError(item_id, f"{attribute.api_name} is derived")
    try:
        return encode_value(attribute.type, raw)
    except EncodingError as error:
        raise ItemEditError(item_id, f"{attribute.api_name}: {error}") from None


def _values(item_id: str, attributes: Mapping[str, Attribute], raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ItemEditError(item_id, "values must be an object")
    by_name = {a.api_name: a for a in attributes.values()}
    encoded: dict[str, Any] = {}
    for name, value in raw.items():
        attribute = attributes.get(name) or by_name.get(name)
        if attribute is None:
            raise ItemEditError(item_id, f"unknown Attribute {name!r}")
        encoded[attribute.id] = _encode(item_id, attribute, value)
    return encoded


def _key(item_id: str, raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ItemEditError(item_id, "keys must be non-empty strings")
    return normalize_key(raw.strip())


def normalize_edits(
    item: ItemRow, edits: Mapping[str, Any] | None, schema: Schema
) -> dict[str, Any]:
    """Edits validated against the schema, with Attribute ids and canonical values."""
    if not edits:
        return {}
    allowed = {
        RECORD_ITEM: {"values"},
        VALUE_ITEM: {"value", "key"},
        LINK_ITEM: {"from", "to", "values"},
    }[item.item_type]
    unknown = sorted(set(edits) - allowed)
    if unknown:
        raise ItemEditError(
            item.item_id, f"{item.item_type} items accept edits of {sorted(allowed)}, not {unknown}"
        )
    attributes = _attributes(schema, item)
    normalized: dict[str, Any] = {}
    if "values" in edits:
        normalized["values"] = _values(item.item_id, attributes, edits["values"])
    if "value" in edits:
        attribute = attributes.get(str(item.subject.get("attribute")))
        if attribute is None:
            raise ItemEditError(item.item_id, "the Attribute no longer exists")
        normalized["value"] = _encode(item.item_id, attribute, edits["value"])
    for name in ("key", "from", "to"):
        if name in edits:
            normalized[name] = _key(item.item_id, edits[name])
    return normalized


def effective_subject(item: ItemRow) -> dict[str, Any]:
    """The subject with the stored reviewer edits applied."""
    subject = dict(item.subject)
    edits = item.edits or {}
    if "values" in edits:
        subject["values"] = {**(subject.get("values") or {}), **edits["values"]}
    for name in ("value", "key", "from", "to"):
        if name in edits:
            subject[name] = edits[name]
    return subject


def summarize(items: Iterable[ItemRow], schema: Schema | None = None) -> dict[str, Any]:
    """Counts of a Proposal's items: per decision, per item type and per Kind or Relation (api
    names when `schema` is given), and the mean confidence."""
    rows = list(items)
    decisions: Counter[str] = Counter({d.value: 0 for d in ItemDecision})
    types: Counter[str] = Counter()
    targets: Counter[str] = Counter()
    confidences = [row.confidence for row in rows if row.confidence is not None]
    for row in rows:
        decisions[row.decision.value] += 1
        types[row.item_type] += 1
        targets[_target_name(row, schema)] += 1
    return {
        "items": len(rows),
        "decisions": dict(decisions),
        "types": dict(sorted(types.items())),
        "targets": dict(sorted(targets.items())),
        "mean_confidence": (round(sum(confidences) / len(confidences), 4) if confidences else None),
    }


def _target_name(row: ItemRow, schema: Schema | None) -> str:
    target = row.target
    if schema is None:
        return target
    kind = schema.kinds.get(target)
    if kind is not None:
        return kind.api_name
    relation = schema.relations.get(target)
    return relation.api_name if relation is not None else target


def proposal_text(summary: Mapping[str, Any], documents: Iterable[str]) -> tuple[str, str]:
    """Title and description of an extraction Proposal from its summary."""
    decisions = summary.get("decisions", {})
    names = sorted(documents)
    shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    title = (
        f"Extract {shown}: {summary.get('items', 0)} item(s), "
        f"{decisions.get('accepted', 0)} accepted, {decisions.get('pending', 0)} pending"
    )
    targets = ", ".join(f"{name} {count}" for name, count in summary.get("targets", {}).items())
    description = (
        f"Proposed from {len(names)} document(s). Items by type: "
        + ", ".join(f"{name} {count}" for name, count in summary.get("types", {}).items())
        + (f"; by Kind or Relation: {targets}" if targets else "")
        + f". Rejected: {decisions.get('rejected', 0)}. Review the items, then merge."
    )
    return title[:300], description
