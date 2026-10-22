"""Rate limit buckets shared by every Keeper, in Postgres (`keeper.limits.rate.backend:
postgres`).

`PostgresRateStore` implements `RateStore` over `lorebase_store.ratelimit`. Requests are served
from tokens leased from the shared bucket (`postgres.lease_tokens`, at most a quarter of the
bucket), so most requests cost no query; a request that finds its lease used up or expired
(`postgres.lease_ttl_s`) takes a new one, one query per bucket at a time. Leases come out of
the shared bucket, so the Keepers together never exceed a limit; tokens of an expired lease
are lost, which only makes it stricter.

Failed-authentication buckets lease one token per failure and are checked before every
authentication from a reading cached for `postgres.check_cache_s`. They fail closed: when the
database cannot be read in `postgres.auth_failures_timeout_s`, a request presenting a
credential is answered 429, unless a reading of its bucket at most `postgres.stale_reading_s`
old still allows it (so an outage refuses unknown clients, not every client). Other buckets
follow `postgres.on_error`: `local` (the default) enforces the limits in this process until
the database answers again, `allow` lets requests through, `deny` refuses them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from opentelemetry import metrics
from opentelemetry.metrics import MeterProvider
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_keeper.ratelimit.bucket import BucketSpec, BucketState, Decision, RateStore, take
from lorebase_keeper.ratelimit.config import PostgresRateSettings
from lorebase_store import ratelimit as buckets

logger = logging.getLogger("lorebase.keeper.limits")

LEASE_SHARE = 4
"""A lease is at most 1/LEASE_SHARE of its bucket, so several Keepers can hold one at once."""
PRUNE_ROUNDS = 10
"""Batches one prune deletes at most."""


@dataclass(slots=True)
class _Local:
    """What this process knows of one shared bucket."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    leased: float = 0.0
    expires: float = float("-inf")
    shared: float = 0.0
    """Tokens left in the shared bucket at the last lease."""
    reading: buckets.BucketReading | None = None
    read_at: float = float("-inf")


def _decision(spec: BucketSpec, tokens: float, allowed: bool, cost: float) -> Decision:
    """A decision with headers computed from the tokens known to be available."""
    tokens = max(0.0, min(float(spec.capacity), tokens))
    retry = 0 if allowed else max(1, math.ceil((cost - tokens) / spec.refill_per_s))
    return Decision(
        allowed=allowed,
        limit=spec.capacity,
        remaining=math.floor(tokens),
        reset_s=math.ceil((spec.capacity - tokens) / spec.refill_per_s),
        retry_after_s=retry,
    )


class PostgresRateStore:
    """`RateStore` over the shared `rate_limit_buckets` table (see the module doc).

    `fail_closed` refuses when the database fails (failed-authentication buckets); otherwise
    `settings.on_error` decides, with `fallback` the store of `local`. `lease_tokens` overrides
    the lease size (1 for failed-authentication buckets). `prune` makes this store delete the
    rows of full buckets now and then.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: PostgresRateSettings,
        *,
        max_buckets: int,
        fallback: RateStore | None = None,
        fail_closed: bool = False,
        lease_tokens: int | None = None,
        prune: bool = False,
        label: str = "principals",
        clock: Callable[[], float] = time.monotonic,
        meter_provider: MeterProvider | None = None,
    ) -> None:
        if not fail_closed and settings.on_error == "local" and fallback is None:
            raise ValueError("on_error local needs a fallback store")
        self._sessions = sessions
        self._settings = settings
        self._max = max_buckets
        self._fallback = fallback
        self._fail_closed = fail_closed
        self._timeout = settings.auth_failures_timeout_s if fail_closed else settings.timeout_s
        self._lease_tokens = lease_tokens or settings.lease_tokens
        self._prune = prune
        self._label = label
        self._clock = clock
        self._local: OrderedDict[str, _Local] = OrderedDict()
        self._last_prune = clock()
        self._pruning: asyncio.Task[None] | None = None
        meter = (meter_provider or metrics.get_meter_provider()).get_meter("lorebase.keeper")
        self._queries = meter.create_counter(
            "lorebase.ratelimit.shared.queries",
            unit="{query}",
            description="Queries to the shared rate limit buckets, by store and outcome.",
        )

    def _entry(self, key: str) -> _Local:
        entry = self._local.get(key)
        if entry is None:
            entry = self._local[key] = _Local()
        self._local.move_to_end(key)
        while len(self._local) > self._max:
            self._local.popitem(last=False)
        return entry

    def _lease_size(self, spec: BucketSpec) -> int:
        return max(1, min(self._lease_tokens, spec.capacity // LEASE_SHARE))

    def _from_lease(self, entry: _Local, cost: float) -> bool:
        if entry.expires > self._clock() and entry.leased >= cost:
            entry.leased -= cost
            return True
        return False

    async def take(self, key: str, spec: BucketSpec, cost: float = 1.0) -> Decision:
        entry = self._entry(key)
        if self._from_lease(entry, cost):
            return _decision(spec, entry.leased + entry.shared, True, cost)
        async with entry.lock:
            if self._from_lease(entry, cost):  # another request leased meanwhile
                return _decision(spec, entry.leased + entry.shared, True, cost)
            self._maybe_prune()
            try:
                async with asyncio.timeout(self._timeout):
                    async with self._sessions() as session, session.begin():
                        lease = await buckets.lease(
                            session,
                            key,
                            capacity=spec.capacity,
                            refill_per_s=spec.refill_per_s,
                            lease=self._lease_size(spec),
                            cost=cost,
                        )
            except Exception as error:
                return await self._failed(error, "take", key, spec, cost)
            self._queries.add(1, {"store": self._label, "outcome": "ok"})
            entry.shared = lease.remaining
            entry.reading = buckets.BucketReading(lease.remaining, lease.now, lease.now)
            entry.read_at = self._clock()
            if lease.granted < cost:
                entry.leased, entry.expires = 0.0, float("-inf")
                return _decision(spec, lease.remaining, False, cost)
            entry.leased = lease.granted - cost
            entry.expires = self._clock() + self._settings.lease_ttl_s
            return _decision(spec, entry.leased + entry.shared, True, cost)

    async def peek(self, key: str, spec: BucketSpec, cost: float = 1.0) -> Decision:
        entry = self._entry(key)
        now = self._clock()
        if entry.reading is None or now - entry.read_at > self._settings.check_cache_s:
            try:
                async with asyncio.timeout(self._timeout):
                    async with self._sessions() as session, session.begin():
                        reading = await buckets.read(session, key)
            except Exception as error:
                stale = entry.reading is not None and (
                    now - entry.read_at <= self._settings.stale_reading_s
                )
                if not stale:
                    return await self._failed(error, "peek", key, spec, cost)
                # A recent reading of this bucket still decides: only clients the database
                # never saw recently are refused while it does not answer.
                self._queries.add(1, {"store": self._label, "outcome": "stale"})
            else:
                self._queries.add(1, {"store": self._label, "outcome": "ok"})
                entry.reading, entry.read_at = reading, self._clock()
        known = entry.reading
        assert known is not None
        state = None if known.tokens is None else BucketState(known.tokens, known.updated)
        _, decision = take(state, spec, known.now + (self._clock() - entry.read_at), cost)
        return decision

    async def _failed(
        self, error: Exception, operation: str, key: str, spec: BucketSpec, cost: float
    ) -> Decision:
        """The decision when the shared buckets cannot be reached."""
        reason = "timeout" if isinstance(error, TimeoutError) else type(error).__name__
        self._queries.add(1, {"store": self._label, "outcome": reason})
        logger.warning(
            "shared rate limit buckets unavailable",
            exc_info=not isinstance(error, TimeoutError),
            extra={"store": self._label, "reason": reason},
        )
        mode = "deny" if self._fail_closed else self._settings.on_error
        if mode == "local" and self._fallback is not None:
            if operation == "take":
                return await self._fallback.take(key, spec, cost)
            return await self._fallback.peek(key, spec, cost)
        if mode == "allow":
            return _decision(spec, float(spec.capacity), True, cost)
        retry = self._settings.error_retry_after_s
        return Decision(False, spec.capacity, 0, retry, retry)

    def _maybe_prune(self) -> None:
        if not self._prune or self._pruning is not None:
            return
        if self._clock() - self._last_prune < self._settings.prune_interval_s:
            return
        self._last_prune = self._clock()
        self._pruning = asyncio.create_task(self._prune_once(), name="ratelimit-prune")

    async def _prune_once(self) -> None:
        try:
            batch = self._settings.prune_batch
            async with asyncio.timeout(max(self._settings.timeout_s, 5.0) * PRUNE_ROUNDS):
                for _ in range(PRUNE_ROUNDS):  # until a batch comes back short
                    async with self._sessions() as session, session.begin():
                        if await buckets.prune(session, limit=batch) < batch:
                            break
        except Exception:
            logger.warning("cannot prune the shared rate limit buckets", exc_info=True)
        finally:
            self._pruning = None

    async def aclose(self) -> None:
        """Stop a running prune."""
        task, self._pruning = self._pruning, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
