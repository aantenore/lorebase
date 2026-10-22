"""Per-process LRU of compiled schemas keyed by Domain and head Revision id.

A Revision is immutable, and so is the schema at it: an entry can never be stale, so the cache
needs no invalidation. Readers look up the branch head first (one indexed read) and only load
the compiled schema on a miss.
"""

from __future__ import annotations

from collections import OrderedDict

from lorebase_core.model import Schema


class SchemaCache:
    """Bounded LRU mapping `(domain_id, revision_id)` to the schema at that Revision.

    Keys are Revision ids only (never schema hashes): callers look a branch head's schema up
    by its head Revision (`reads.head_schema`). The same object comes back for the same
    Revision, which keeps caches keyed by schema identity (Keeper's effective sensitivity
    tags) effective.
    """

    def __init__(self, entries: int) -> None:
        self._entries = entries
        self._items: OrderedDict[tuple[str, bytes], Schema] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, domain_id: str, revision_id: bytes) -> Schema | None:
        key = (domain_id, revision_id)
        found = self._items.get(key)
        if found is None:
            self.misses += 1
            return None
        self.hits += 1
        self._items.move_to_end(key)
        return found

    def put(self, domain_id: str, revision_id: bytes, schema: Schema) -> None:
        if self._entries == 0:
            return
        key = (domain_id, revision_id)
        self._items[key] = schema
        self._items.move_to_end(key)
        while len(self._items) > self._entries:
            self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)
