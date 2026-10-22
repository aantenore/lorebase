"""Pure mapping from tree content to materialized rows, shared by incremental apply and rebuild.

Both paths must produce identical rows; everything that decides a column value lives here.

Lineage of a row maps each Attribute holding a value to the provenance entry of its last
change: the provenance maps of the element's changes are folded oldest first and the result
is restricted to the Attributes present in the current values. Changes that touch no
Attribute (deletions, schema elements) are attributed under `ELEMENT_PROVENANCE_KEY`.
The Revisions of those changes are folded the same way (`next_lineage_revisions`), with the
Revision that created the element under `CREATED_KEY`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from lorebase_core.model import Kind, SearchWeight
from lorebase_core.tree import LINK_KEY_SEPARATOR, PARTITION_SCHEMA
from lorebase_core.types import AttributeType, to_text

ELEMENT_PROVENANCE_KEY = "$element"
"""Provenance map key of changes that touch no Attribute value."""

CREATED_KEY = "$created"
"""Lineage revisions key of the Revision that last created the element."""

DEFAULT_SEARCH_WEIGHT = SearchWeight.B
"""Weight of searchable Attributes without an explicit `search_weight` (titles use A)."""

_KIND_PREFIX = "k:"
_RELATION_PREFIX = "r:"


class PartitionType(StrEnum):
    SCHEMA = "schema"
    KIND = "kind"
    RELATION = "relation"


@dataclass(frozen=True, slots=True)
class PartitionId:
    """A parsed partition id: its type and the Kind or Relation id it holds."""

    type: PartitionType
    element_id: str


def parse_partition(partition: str) -> PartitionId:
    """Inverse of `kind_partition`, `relation_partition` and `PARTITION_SCHEMA`."""
    if partition == PARTITION_SCHEMA:
        return PartitionId(PartitionType.SCHEMA, "")
    if partition.startswith(_KIND_PREFIX):
        return PartitionId(PartitionType.KIND, partition[len(_KIND_PREFIX) :])
    if partition.startswith(_RELATION_PREFIX):
        return PartitionId(PartitionType.RELATION, partition[len(_RELATION_PREFIX) :])
    raise ValueError(f"unknown partition {partition!r}")


def split_link_key(element_key: str) -> tuple[str, str]:
    """`(from_key, to_key)` of a Link element key."""
    from_key, separator, to_key = element_key.partition(LINK_KEY_SEPARATOR)
    if not separator:
        raise ValueError(f"not a Link key: {element_key!r}")
    return from_key, to_key


def changed_attributes(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> list[str]:
    """Attributes whose value differs between two value maps (absent counts as a value)."""
    previous = old or {}
    return sorted(
        attribute
        for attribute in previous.keys() | new.keys()
        if previous.get(attribute) != new.get(attribute)
    )


def next_lineage(
    previous: Mapping[str, str] | None,
    provenance: Mapping[str, str] | None,
    values: Mapping[str, Any],
) -> dict[str, str]:
    """Lineage after a change: previous lineage updated with the change's provenance map."""
    merged = dict(previous or {})
    merged.update(provenance or {})
    return {attribute: entry for attribute, entry in sorted(merged.items()) if attribute in values}


def next_lineage_revisions(
    previous: Mapping[str, str] | None,
    provenance: Mapping[str, str] | None,
    revision: bytes,
    values: Mapping[str, Any],
    *,
    created: bool,
) -> dict[str, str]:
    """Lineage revisions after a change made by `revision` (hex values, see module doc).

    Attributes the change attributes take `revision`; the others keep their previous entry.
    `previous` is None when unknown (rows written before the revisions were kept): only the
    change's own entries are known then, and readers resolve the rest from the history.
    """
    merged = {} if created else dict(previous or {})
    merged.update(dict.fromkeys(provenance or {}, revision.hex()))
    if created:
        merged[CREATED_KEY] = revision.hex()
    return {
        attribute: entry
        for attribute, entry in sorted(merged.items())
        if attribute in values or attribute == CREATED_KEY
    }


def record_title(kind: Kind | None, values: Mapping[str, Any]) -> str | None:
    """Text of the Kind's title Attribute, if any. A sensitive title Attribute gives no title:
    titles are shown and searched without the clearance its values need (design 10.2)."""
    if kind is None or kind.title_attribute is None:
        return None
    value = values.get(kind.title_attribute)
    attribute = kind.attributes.get(kind.title_attribute)
    if value is None or attribute is None or attribute.is_sensitive:
        return None
    if attribute.type.name in (AttributeType.JSON, AttributeType.ARRAY):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return to_text(attribute.type, value)


def search_weights(kind: Kind | None) -> list[tuple[str, str]]:
    """`(attribute_id, weight)` of the Kind's searchable Attributes, by Attribute id.

    Sensitive Attributes are left out, like from embedding templates: the full-text index and
    its snippets serve every reader of the Kind, cleared for sensitive values or not.
    """
    if kind is None:
        return []
    return [
        (attribute.id, str(attribute.search_weight or DEFAULT_SEARCH_WEIGHT))
        for attribute in sorted(kind.attributes.values(), key=lambda a: a.id)
        if attribute.searchable and not attribute.is_sensitive
    ]
