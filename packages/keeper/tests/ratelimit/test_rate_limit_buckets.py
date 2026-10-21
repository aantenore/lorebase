"""Token bucket arithmetic, the in-memory store, request classes and the throttle watch."""

from __future__ import annotations

import ipaddress

import pytest

from lorebase_keeper.ratelimit.bucket import BucketSpec, BucketState, InMemoryRateStore, take
from lorebase_keeper.ratelimit.config import (
    BucketSettings,
    ClassOverrides,
    QuotaSettings,
    RateLimitSettings,
    RouteClass,
)
from lorebase_keeper.ratelimit.limiter import RateLimiter, ThrottleWatch
from lorebase_keeper.ratelimit.quotas import QuotaSnapshot

SPEC = BucketSpec(capacity=4, refill_per_s=2.0)


def test_a_new_bucket_is_full_and_a_request_takes_one_token() -> None:
    state, decision = take(None, SPEC, now=100.0)
    assert state == BucketState(tokens=3.0, updated=100.0)
    assert decision.allowed
    assert (decision.limit, decision.remaining) == (4, 3)
    assert decision.reset_s == 1  # half a second per token, rounded up to whole seconds
    assert decision.retry_after_s == 0


def test_the_burst_is_spent_and_then_requests_are_refused() -> None:
    state: BucketState | None = None
    results = []
    for _ in range(6):
        state, decision = take(state, SPEC, now=0.0)
        results.append((decision.allowed, decision.remaining))
    assert results == [(True, 3), (True, 2), (True, 1), (True, 0), (False, 0), (False, 0)]


def test_a_refused_request_costs_nothing_and_says_when_to_retry() -> None:
    state = BucketState(tokens=0.25, updated=10.0)
    after, decision = take(state, SPEC, now=10.0)
    assert not decision.allowed
    assert after.tokens == pytest.approx(0.25)
    assert decision.retry_after_s == 1  # 0.75 tokens at 2 per second, rounded up
    slow = BucketSpec(capacity=1, refill_per_s=1 / 60)
    _, decision = take(BucketState(0.0, 0.0), slow, now=0.0)
    assert decision.retry_after_s == 60


def test_tokens_refill_with_time_and_never_beyond_the_capacity() -> None:
    state = BucketState(tokens=0.0, updated=0.0)
    after, decision = take(state, SPEC, now=1.0)  # two tokens came back, one is taken
    assert after.tokens == pytest.approx(1.0)
    assert decision.remaining == 1
    full, decision = take(BucketState(0.0, 0.0), SPEC, now=3600.0)
    assert full.tokens == pytest.approx(3.0)
    assert decision.remaining == 3
    assert decision.reset_s == 1


def test_a_clock_that_goes_back_does_not_remove_tokens() -> None:
    after, decision = take(BucketState(tokens=2.0, updated=50.0), SPEC, now=40.0)
    assert decision.allowed
    assert after.tokens == pytest.approx(1.0)


def test_spec_from_settings_converts_requests_per_minute() -> None:
    spec = BucketSpec.from_settings(BucketSettings(requests_per_minute=120, burst=10))
    assert spec == BucketSpec(capacity=10, refill_per_s=2.0)


async def test_the_store_keeps_one_bucket_per_key_and_forgets_the_least_recently_used() -> None:
    clock = [0.0]
    store = InMemoryRateStore(max_buckets=2, clock=lambda: clock[0])
    one = BucketSpec(capacity=1, refill_per_s=0.001)
    assert (await store.take("a", one)).allowed
    assert not (await store.take("a", one)).allowed
    assert (await store.take("b", one)).allowed  # another key has its own bucket
    assert (await store.take("c", one)).allowed  # evicts "a", the least recently used
    assert len(store) == 2
    assert (await store.take("a", one)).allowed  # forgotten: full again


async def test_peeking_at_a_bucket_takes_nothing() -> None:
    store = InMemoryRateStore(max_buckets=10, clock=lambda: 0.0)
    one = BucketSpec(capacity=1, refill_per_s=0.001)
    for _ in range(3):
        assert (await store.peek("a", one)).allowed
    assert (await store.take("a", one)).allowed
    refused = await store.peek("a", one)
    assert not refused.allowed
    assert refused.retry_after_s >= 1


def test_auth_failure_buckets_have_defaults() -> None:
    settings = RateLimitSettings()
    assert (settings.auth_failures.requests_per_minute, settings.auth_failures.burst) == (30, 30)
    assert settings.auth_failures.ipv6_prefix_length == 64


def test_auth_failure_keys_are_the_address_and_the_key_prefix() -> None:
    found = limiter(auth_failures={"ipv6_prefix_length": 48})
    assert found.failure_keys(ipaddress.ip_address("192.0.2.1"), "abcdefghij") == (
        "auth_failures|addr:192.0.2.1",
        "auth_failures|prefix:abcdefghij",
    )
    assert found.failure_keys(ipaddress.ip_address("2001:db8:1:2::9"), None) == (
        "auth_failures|addr:2001:db8:1::/48",
    )
    assert found.failure_keys(None, None) == ()


async def test_failures_are_refused_when_any_bucket_is_empty() -> None:
    found = limiter(auth_failures={"requests_per_minute": 0.001, "burst": 2})
    both = found.failure_keys(ipaddress.ip_address("192.0.2.1"), "abcdefghij")
    other_address = found.failure_keys(ipaddress.ip_address("192.0.2.2"), "abcdefghij")
    assert await found.failures_exhausted(both) is None
    await found.record_failure(both)
    await found.record_failure(both)
    refused = await found.failures_exhausted(both)
    assert refused is not None
    assert refused.retry_after_s >= 1
    assert await found.failures_exhausted(other_address) is not None  # the prefix is spent
    assert await found.failures_exhausted(other_address[:1]) is None


def limiter(**overrides: object) -> RateLimiter:
    settings = RateLimitSettings(**overrides)  # type: ignore[arg-type]
    return RateLimiter(settings, InMemoryRateStore(settings.max_buckets))


@pytest.mark.parametrize(
    ("method", "path", "tags", "expected"),
    [
        ("GET", "/domains/{domain_id}/kinds/{kind}/records", ["read"], RouteClass.READS),
        ("GET", "/domains/{domain_id}/audit", ["admin"], RouteClass.ADMIN),
        ("POST", "/domains/{domain_id}/api-keys", ["admin"], RouteClass.ADMIN),
        ("POST", "/domains/{domain_id}/query", ["read"], RouteClass.READS),
        ("POST", "/domains/{domain_id}/traverse", ["read"], RouteClass.READS),
        ("POST", "/domains/{domain_id}/neighborhood", ["read"], RouteClass.READS),
        ("POST", "/domains/{domain_id}/search", ["read"], RouteClass.SEARCHES),
        ("POST", "/domains/{domain_id}/context", ["agents"], RouteClass.CONTEXT),
        ("POST", "/domains/{domain_id}/operations/{op}/execute", ["operations"], RouteClass.WRITES),
        ("PATCH", "/domains/{domain_id}", ["domains"], RouteClass.WRITES),
        ("DELETE", "/domains/{domain_id}/webhooks/{id}", ["webhooks"], RouteClass.WRITES),
        ("GET", "/auth/whoami", ["auth"], RouteClass.READS),
    ],
)
def test_requests_are_sorted_into_classes(
    method: str, path: str, tags: list[str], expected: RouteClass
) -> None:
    assert limiter().classify(method, path, tags) is expected


def test_the_class_rules_come_from_settings() -> None:
    custom = limiter(routes={"admin_tags": ["ops"], "search_suffixes": ["/find"]})
    assert custom.classify("GET", "/x", ["admin"]) is RouteClass.READS
    assert custom.classify("GET", "/x", ["ops"]) is RouteClass.ADMIN
    assert custom.classify("POST", "/domains/{d}/find", []) is RouteClass.SEARCHES
    assert custom.classify("POST", "/domains/{d}/search", []) is RouteClass.WRITES


async def test_classes_and_principals_have_separate_buckets() -> None:
    tight = {"requests_per_minute": 0.001, "burst": 1}
    found = limiter(reads=tight, writes=tight)
    assert (await found.check("key:a", RouteClass.READS, None)).allowed
    assert not (await found.check("key:a", RouteClass.READS, None)).allowed
    assert (await found.check("key:a", RouteClass.WRITES, None)).allowed
    assert (await found.check("key:b", RouteClass.READS, None)).allowed


async def test_a_domain_override_replaces_the_bucket_and_does_not_share_it() -> None:
    tight = {"requests_per_minute": 0.001, "burst": 1}
    found = limiter(
        reads={"requests_per_minute": 0.001, "burst": 2}, domains={"acme": {"reads": tight}}
    )
    assert (await found.check("key:a", RouteClass.READS, "acme")).limit == 1
    assert not (await found.check("key:a", RouteClass.READS, "acme")).allowed
    # other Domains, and requests without a Domain, use the installation-wide bucket
    assert (await found.check("key:a", RouteClass.READS, "globex")).limit == 2
    assert (await found.check("key:a", RouteClass.READS, None)).allowed
    assert not (await found.check("key:a", RouteClass.READS, "globex")).allowed  # same bucket


def test_domain_overrides_only_replace_the_classes_they_name() -> None:
    settings = RateLimitSettings(
        domains={"acme": ClassOverrides(writes=BucketSettings(requests_per_minute=6, burst=3))}
    )
    assert settings.bucket(RouteClass.WRITES, "acme").burst == 3
    assert settings.bucket(RouteClass.READS, "acme") == settings.reads
    assert settings.bucket(RouteClass.WRITES, "globex") == settings.writes
    assert settings.has_override(RouteClass.WRITES, "acme")
    assert not settings.has_override(RouteClass.READS, "acme")


def test_the_watch_reports_every_threshold_refusals_within_the_window() -> None:
    now = [0.0]
    watch = ThrottleWatch(threshold=3, window_s=10, max_tracked=10, clock=lambda: now[0])
    assert [watch.record("a") for _ in range(2)] == [None, None]
    assert watch.record("b") is None  # counted apart
    assert watch.record("a") == 3
    assert watch.record("a") is None  # counting starts again
    now[0] = 100.0
    assert [watch.record("a"), watch.record("a")] == [None, None]  # the old ones expired
    assert watch.record("a") == 3


def test_the_watch_can_be_turned_off_and_is_bounded() -> None:
    assert ThrottleWatch(threshold=0, window_s=10, max_tracked=10).record("a") is None
    watch = ThrottleWatch(threshold=2, window_s=10, max_tracked=1, clock=lambda: 0.0)
    watch.record("a")
    watch.record("b")  # drops "a"
    assert watch.record("a") is None


def test_quota_limits_come_from_the_defaults_and_per_key_overrides() -> None:
    settings = QuotaSettings(
        requests_per_day=100,
        context_tokens_per_day=1000,
        keys={
            "key_1": {"requests_per_day": 5},
            "key_2": {"requests_per_day": None, "context_tokens_per_day": 7},
        },  # type: ignore[dict-item]
    )
    assert (settings.requests_limit("key_0"), settings.tokens_limit("key_0")) == (100, 1000)
    assert (settings.requests_limit("key_1"), settings.tokens_limit("key_1")) == (5, 1000)
    assert (settings.requests_limit("key_2"), settings.tokens_limit("key_2")) == (None, 7)


def test_a_snapshot_names_the_quota_that_is_used_up() -> None:
    from datetime import UTC, date, datetime

    def snapshot(**use: int | None) -> QuotaSnapshot:
        values: dict[str, int | None] = {
            "requests_used": 0,
            "requests_limit": 10,
            "tokens_used": 0,
            "tokens_limit": 100,
            **use,
        }
        return QuotaSnapshot(
            day=date(2026, 10, 20),
            resets_at=datetime(2026, 10, 21, tzinfo=UTC),
            **values,  # type: ignore[arg-type]
        )

    assert snapshot().exceeded() is None
    assert snapshot(requests_used=10).exceeded() == "requests"
    assert snapshot(tokens_used=100).exceeded() == "context_tokens"
    assert snapshot(requests_used=10**9, requests_limit=None).exceeded() is None
