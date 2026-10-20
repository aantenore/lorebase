"""A small in-process cache with a time to live."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable


class TtlCache[K, V]:
    """Entries expire `ttl_s` after being stored; the oldest are dropped beyond `max_entries`.

    A `ttl_s` of 0 stores nothing. Not shared between processes: another process sees a
    change after at most one time to live.
    """

    def __init__(
        self,
        ttl_s: float,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_s
        self._max = max_entries
        self._clock = clock
        self._entries: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def get(self, key: K) -> V | None:
        """The stored value, or None when absent or expired."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires <= self._clock():
            del self._entries[key]
            return None
        return value

    def set(self, key: K, value: V) -> None:
        """Store a value."""
        if self._ttl <= 0:
            return
        self._entries[key] = (self._clock() + self._ttl, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        """Forget everything (after a local change)."""
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
