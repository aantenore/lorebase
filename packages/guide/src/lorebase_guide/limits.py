"""Per-process limits that keep one caller from spending the model budget of everyone.

Keyed by the caller's verified identity (the principal Keeper reported, never the
credential): a sliding one-minute window of starts, and the number of explorations running at
once, per caller and in total. Callers are checked by Keeper before any of these is touched,
so unverified credentials cannot fill the maps; the window map is bounded all the same, and a
new caller is refused while it is full of callers seen within the last minute.
With several replicas each one enforces its own limits; Keeper's own rate limits apply to the
reads on top.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

_WINDOW_S = 60.0


@dataclass(frozen=True)
class Refusal:
    reason: str
    retry_after_s: int


class SlidingWindow:
    """At most `limit` events per key within a minute."""

    def __init__(
        self,
        limit: int,
        clock: Callable[[], float] = time.monotonic,
        *,
        max_keys: int = 10_000,
    ) -> None:
        self._limit = limit
        self._clock = clock
        self._max_keys = max_keys
        self._events: dict[str, deque[float]] = {}

    def take(self, key: str) -> Refusal | None:
        now = self._clock()
        events = self._events.get(key)
        if events is None:
            if len(self._events) >= self._max_keys:
                self._prune(now)
            if len(self._events) >= self._max_keys:
                return Refusal("busy", int(_WINDOW_S))
            events = self._events[key] = deque()
        while events and events[0] <= now - _WINDOW_S:
            events.popleft()
        if len(events) >= self._limit:
            return Refusal("rate", max(1, int(events[0] + _WINDOW_S - now) + 1))
        events.append(now)
        return None

    def _prune(self, now: float) -> None:
        for key in [k for k, v in self._events.items() if not v or v[-1] <= now - _WINDOW_S]:
            del self._events[key]

    def __len__(self) -> int:
        return len(self._events)


class Slots:
    """Concurrency: at most `total` running, `per_key` of them for one key."""

    def __init__(self, total: int, per_key: int) -> None:
        self._total = total
        self._per_key = per_key
        self._running: dict[str, int] = {}

    def acquire(self, key: str) -> Refusal | None:
        if sum(self._running.values()) >= self._total:
            return Refusal("busy", 5)
        if self._running.get(key, 0) >= self._per_key:
            return Refusal("concurrent", 5)
        self._running[key] = self._running.get(key, 0) + 1
        return None

    def release(self, key: str) -> None:
        count = self._running.get(key, 0) - 1
        if count > 0:
            self._running[key] = count
        else:
            self._running.pop(key, None)

    @property
    def running(self) -> int:
        return sum(self._running.values())


class Gate:
    """At most `size` holders at once; `enter` never waits."""

    def __init__(self, size: int) -> None:
        self._size = size
        self._held = 0

    def enter(self) -> bool:
        if self._held >= self._size:
            return False
        self._held += 1
        return True

    def leave(self) -> None:
        self._held = max(0, self._held - 1)

    @property
    def held(self) -> int:
        return self._held
