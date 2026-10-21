"""Rate limiter: sorts requests into classes, takes from the principal's bucket, counts refusals.

A principal has one bucket per class (`reads`, `writes`, `searches`, `context`, `admin`), keyed
by the API key id or, for users, the principal id. A Domain that overrides the bucket of a
class gets a bucket of its own for it.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable, Collection

from opentelemetry import metrics
from opentelemetry.metrics import MeterProvider

from lorebase_keeper.ratelimit.bucket import BucketSpec, Decision, RateStore
from lorebase_keeper.ratelimit.config import RateLimitSettings, RouteClass

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class RateLimiter:
    """Applies `RateLimitSettings` on top of a `RateStore`."""

    def __init__(
        self,
        settings: RateLimitSettings,
        store: RateStore,
        *,
        meter_provider: MeterProvider | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
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
