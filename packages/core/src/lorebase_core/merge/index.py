"""An ``IndexView`` read from a Revision tree, for targets without a materialized head.

Each partition is loaded on first use and indexed in memory, so lookups cost one scan of the
partitions they touch. A materialized head (design 6.11) answers the same questions with
indexed reads.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from pydantic import JsonValue

from lorebase_core.canonical import canonical_bytes
from lorebase_core.merge.objects import ObjectStore, load_object
from lorebase_core.tree import NodeStore, kind_partition, relation_partition, tree_entries
from lorebase_core.types import CanonicalValue

__all__ = ["TreeIndexView"]


class _Links:
    def __init__(self) -> None:
        self.pairs: set[tuple[str, str]] = set()
        self.by_from: dict[str, set[str]] = defaultdict(set)
        self.by_to: dict[str, set[str]] = defaultdict(set)


class TreeIndexView:
    """:class:`~lorebase_core.validate.IndexView` over the tree at ``root``."""

    def __init__(self, store: NodeStore, objects: ObjectStore, root: bytes | None) -> None:
        self._store = store
        self._objects = objects
        self._root = root
        self._records: dict[str, dict[str, dict[str, JsonValue]]] = {}
        self._links: dict[str, _Links] = {}

    def _kind(self, kind_id: str) -> dict[str, dict[str, JsonValue]]:
        records = self._records.get(kind_id)
        if records is None:
            records = {}
            for key, digest in tree_entries(self._store, self._root, kind_partition(kind_id)):
                values = load_object(self._objects, digest).get("values")
                records[key] = dict(values) if isinstance(values, dict) else {}
            self._records[kind_id] = records
        return records

    def _relation(self, relation_id: str) -> _Links:
        links = self._links.get(relation_id)
        if links is None:
            links = _Links()
            for _, digest in tree_entries(self._store, self._root, relation_partition(relation_id)):
                payload = load_object(self._objects, digest)
                start, end = str(payload["from"]), str(payload["to"])
                links.pairs.add((start, end))
                links.by_from[start].add(end)
                links.by_to[end].add(start)
            self._links[relation_id] = links
        return links

    def record_exists(self, kind_id: str, key: str) -> bool:
        return key in self._kind(kind_id)

    def find_unique(self, kind_id: str, attribute_id: str, value: CanonicalValue) -> str | None:
        wanted = canonical_bytes(value)
        for key in sorted(self._kind(kind_id)):
            found = self._kind(kind_id)[key].get(attribute_id)
            if found is not None and canonical_bytes(found) == wanted:
                return key
        return None

    def keys_referencing(self, kind_id: str, attribute_id: str, key: str) -> Iterable[str]:
        for record_key, values in sorted(self._kind(kind_id).items()):
            value = values.get(attribute_id)
            if value == key or (isinstance(value, list) and key in value):
                yield record_key

    def link_exists(self, relation_id: str, from_key: str, to_key: str) -> bool:
        return (from_key, to_key) in self._relation(relation_id).pairs

    def count_links(
        self, relation_id: str, *, from_key: str | None = None, to_key: str | None = None
    ) -> int:
        return len(list(self.links_of(relation_id, from_key=from_key, to_key=to_key)))

    def links_of(
        self, relation_id: str, *, from_key: str | None = None, to_key: str | None = None
    ) -> Iterable[tuple[str, str]]:
        links = self._relation(relation_id)
        if from_key is not None:
            pairs = {(from_key, end) for end in links.by_from.get(from_key, ())}
        elif to_key is not None:
            pairs = {(start, to_key) for start in links.by_to.get(to_key, ())}
        else:
            pairs = set(links.pairs)
        if from_key is not None and to_key is not None:
            pairs = {p for p in pairs if p[1] == to_key}
        return sorted(pairs)
