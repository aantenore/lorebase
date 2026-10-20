from __future__ import annotations

import hashlib
import itertools
from collections import Counter

from lorebase_core.tree import InMemoryNodeStore, key_hash


class CountingStore(InMemoryNodeStore):
    """In-memory store that counts reads per node hash."""

    def __init__(self) -> None:
        super().__init__()
        self.reads: Counter[bytes] = Counter()

    def get(self, node_hash: bytes) -> bytes | None:
        self.reads[node_hash] += 1
        return super().get(node_hash)

    @property
    def total_reads(self) -> int:
        return sum(self.reads.values())


def value(label: object) -> bytes:
    """A deterministic 32-byte object hash for test entries."""
    return hashlib.sha256(repr(label).encode()).digest()


def keys_with_prefix(prefix: bytes, count: int, stem: str = "key") -> list[str]:
    """Find ``count`` keys whose key hash starts with ``prefix``."""
    found: list[str] = []
    for i in itertools.count():
        candidate = f"{stem}-{i}"
        if key_hash(candidate).startswith(prefix):
            found.append(candidate)
            if len(found) == count:
                return found
    raise AssertionError("unreachable")
