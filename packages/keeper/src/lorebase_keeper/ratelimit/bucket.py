"""Token bucket arithmetic and the storage protocol behind it.

A bucket holds up to `capacity` tokens and gains `refill_per_s` tokens each second; a request
takes one. Time comes from the caller, so the math is deterministic and a shared store (Valkey,
Redis) can run the same function server side.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from lorebase_keeper.ratelimit.config import BucketSettings


@dataclass(frozen=True, slots=True)
class BucketSpec:
    """Size and refill rate of a bucket."""

    capacity: int
    refill_per_s: float

    @classmethod
    def from_settings(cls, settings: BucketSettings) -> BucketSpec:
        return cls(capacity=settings.burst, refill_per_s=settings.requests_per_minute / 60)


@dataclass(slots=True)
class BucketState:
    """Tokens a bucket held at `updated` (a reading of the store's clock, in seconds)."""

    tokens: float
    updated: float


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of taking from a bucket, in the terms of the `RateLimit-*` headers.

    `reset_s` is the time until the bucket is full again; `retry_after_s` the time until the
    request would be allowed (0 when it was).
    """

    allowed: bool
    limit: int
    remaining: int
    reset_s: int
    retry_after_s: int = 0


def take(
    state: BucketState | None, spec: BucketSpec, now: float, cost: float = 1.0
) -> tuple[BucketState, Decision]:
    """Refill the bucket to `now`, then take `cost` tokens if there are enough.

    A bucket that was never used is full. Tokens are only taken from a bucket that holds at
    least `cost`, so a refused request costs nothing.
    """
    if state is None:
        tokens = float(spec.capacity)
    else:
        elapsed = max(0.0, now - state.updated)
        tokens = min(float(spec.capacity), state.tokens + elapsed * spec.refill_per_s)
    allowed = tokens >= cost
    if allowed:
        tokens -= cost
    reset = math.ceil((spec.capacity - tokens) / spec.refill_per_s)
    retry = 0 if allowed else max(1, math.ceil((cost - tokens) / spec.refill_per_s))
    decision = Decision(
        allowed=allowed,
        limit=spec.capacity,
        remaining=max(0, math.floor(tokens)),
        reset_s=reset,
        retry_after_s=retry,
    )
    return BucketState(tokens, now), decision


class RateStore(Protocol):
    """Keeps bucket state. `take` is atomic per key: concurrent requests of one principal see
    each other's tokens."""

    async def take(self, key: str, spec: BucketSpec, cost: float = 1.0) -> Decision: ...


class InMemoryRateStore:
    """Buckets in this process, the least recently used dropped beyond `max_buckets`.

    Nothing awaits between reading and writing a bucket, so on one event loop `take` is atomic.
    """

    def __init__(self, max_buckets: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._max = max_buckets
        self._clock = clock
        self._buckets: OrderedDict[str, BucketState] = OrderedDict()

    def __len__(self) -> int:
        return len(self._buckets)

    async def take(self, key: str, spec: BucketSpec, cost: float = 1.0) -> Decision:
        state, decision = take(self._buckets.get(key), spec, self._clock(), cost)
        self._buckets[key] = state
        self._buckets.move_to_end(key)
        while len(self._buckets) > self._max:
            self._buckets.popitem(last=False)
        return decision
