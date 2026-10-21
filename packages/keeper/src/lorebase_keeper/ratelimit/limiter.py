"""Rate limiter: sorts requests into classes, takes from the principal's bucket, counts refusals.

A principal has one bucket per class (`reads`, `writes`, `searches`, `context`, `admin`), keyed
by the API key id or, for users, the principal id. A Domain that overrides the bucket of a
class gets a bucket of its own for it.

Failed authentications have buckets of their own (`auth_failures`), in a separate store so
that unauthenticated traffic cannot push principals' buckets out: one per client address (IPv6
by network) and one per presented API key prefix.
"""

from __future__ import annotations

import ipaddress
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Collection, Sequence

from opentelemetry import metrics
from opentelemetry.metrics import MeterProvider

from lorebase_keeper.client_address import IPAddress
from lorebase_keeper.ratelimit.bucket import BucketSpec, Decision, InMemoryRateStore, RateStore
from lorebase_keeper.ratelimit.config import RateLimitSettings, RouteClass

AUTH_FAILURES = "auth_failures"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class RateLimiter:
    """Applies `RateLimitSettings` on top of a `RateStore`."""

    def __init__(
        self,
        settings: RateLimitSettings,
        store: RateStore,
        *,
        failure_store: RateStore | None = None,
        meter_provider: MeterProvider | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._failure_store = failure_store or InMemoryRateStore(settings.max_buckets)
        self._failure_spec = BucketSpec.from_settings(settings.auth_failures)
        meter = (meter_provider or metrics.get_meter_provider()).get_meter("lorebase.keeper")
        self._limited = meter.create_counter(
            "lorebase.http.rate_limited",
            unit="{request}",
            description="Requests refused with 429 because a rate limit was reached.",
        )

    def classify(self, method: str, route_path: str, tags: Collection[str] = ()) -> RouteClass:
        """The class of a request, from its method, route template and the route's tags."""
        routes = self._settings.routes
        if not set(tags).isdisjoint(routes.admin_tags):
            return RouteClass.ADMIN
        if method in SAFE_METHODS:
            return RouteClass.READS
        if method == "POST":
            if route_path.endswith(routes.search_suffixes):
                return RouteClass.SEARCHES
            if route_path.endswith(routes.context_suffixes):
                return RouteClass.CONTEXT
            if route_path.endswith(routes.read_post_suffixes):
                return RouteClass.READS
        return RouteClass.WRITES

    async def check(self, who: str, route_class: RouteClass, domain_id: str | None) -> Decision:
        """Take one token from the bucket of `who` for this class (and Domain override)."""
        bucket = self._settings.bucket(route_class, domain_id)
        key = f"{who}|{route_class.value}"
        if self._settings.has_override(route_class, domain_id):
            key += f"|domain:{domain_id}"
        decision = await self._store.take(key, BucketSpec.from_settings(bucket))
        if not decision.allowed:
            self._limited.add(1, {"class": route_class.value})
        return decision

    def failure_keys(self, address: IPAddress | None, key_prefix: str | None) -> tuple[str, ...]:
        """The failed-authentication buckets of a request: its client address (an IPv6
        address by its network) and its API key prefix, each when known."""
        keys: list[str] = []
        if isinstance(address, ipaddress.IPv6Address):
            length = self._settings.auth_failures.ipv6_prefix_length
            keys.append(f"addr:{ipaddress.ip_network(f'{address}/{length}', strict=False)}")
        elif address is not None:
            keys.append(f"addr:{address}")
        if key_prefix is not None:
            keys.append(f"prefix:{key_prefix}")
        return tuple(f"{AUTH_FAILURES}|{key}" for key in keys)

    async def failures_exhausted(self, keys: Sequence[str]) -> Decision | None:
        """The refusal (the longest wait) when one of the buckets has no failure left, else
        None. Takes nothing."""
        refused: Decision | None = None
        for key in keys:
            decision = await self._failure_store.peek(key, self._failure_spec)
            if not decision.allowed and (
                refused is None or decision.retry_after_s > refused.retry_after_s
            ):
                refused = decision
        if refused is not None:
            self._limited.add(1, {"class": AUTH_FAILURES})
        return refused

    async def record_failure(self, keys: Sequence[str]) -> None:
        """Take one token from each bucket for a failed authentication."""
        for key in keys:
            await self._failure_store.take(key, self._failure_spec)


class ThrottleWatch:
    """Counts the refusals of each principal in a sliding window.

    `record` returns the count when it reaches the threshold and starts counting again, so a
    principal that keeps hammering the API produces one report per `threshold` refusals.
    """

    def __init__(
        self,
        threshold: int,
        window_s: float,
        max_tracked: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._window_s = window_s
        self._max = max_tracked
        self._clock = clock
        self._seen: OrderedDict[str, deque[float]] = OrderedDict()

    def record(self, who: str) -> int | None:
        """Note a refusal of `who`; the number of refusals in the window when it is time to
        report them."""
        if self._threshold <= 0:
            return None
        now = self._clock()
        times = self._seen.pop(who, None) or deque()
        while times and now - times[0] > self._window_s:
            times.popleft()
        times.append(now)
        if len(times) >= self._threshold:
            return len(times)
        self._seen[who] = times
        while len(self._seen) > self._max:
            self._seen.popitem(last=False)
        return None
