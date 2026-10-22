"""The shared bucket store when its database fails or is slow."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_keeper.ratelimit.bucket import BucketSpec, InMemoryRateStore
from lorebase_keeper.ratelimit.config import PostgresRateSettings
from lorebase_keeper.ratelimit.shared import PostgresRateStore

SPEC = BucketSpec(capacity=2, refill_per_s=0.001)


class Unreachable:
    """A session factory whose sessions fail, or hang (`slow`)."""

    def __init__(self, *, slow: bool = False) -> None:
        self.slow = slow
        self.opened = 0

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        self.opened += 1
        if self.slow:
            await asyncio.sleep(10)
        raise ConnectionError("database unreachable")
        yield  # pragma: no cover

    def __call__(self) -> Any:
        return self._session()


def store(sessions: Unreachable, **settings: Any) -> PostgresRateStore:
    fail_closed = settings.pop("fail_closed", False)
    return PostgresRateStore(
        cast("async_sessionmaker[AsyncSession]", sessions),
        PostgresRateSettings(**settings),
        max_buckets=100,
        fallback=InMemoryRateStore(100),
        fail_closed=fail_closed,
    )


async def test_local_fallback_keeps_enforcing_limits() -> None:
    shared = store(Unreachable())
    decisions = [await shared.take("k", SPEC) for _ in range(3)]
    assert [d.allowed for d in decisions] == [True, True, False]


@pytest.mark.parametrize(("mode", "allowed"), [("allow", True), ("deny", False)])
async def test_allow_and_deny(mode: str, allowed: bool) -> None:
    shared = store(Unreachable(), on_error=mode, error_retry_after_s=3)
    decision = await shared.take("k", SPEC)
    assert decision.allowed is allowed
    if not allowed:
        assert decision.retry_after_s == 3


async def test_failed_authentication_buckets_fail_closed_when_the_database_is_slow() -> None:
    sessions = Unreachable(slow=True)
    failures = store(sessions, fail_closed=True, timeout_s=0.01, on_error="allow")
    assert not (await failures.peek("auth_failures|addr:192.0.2.1", SPEC)).allowed
    assert not (await failures.take("auth_failures|addr:192.0.2.1", SPEC)).allowed
    assert sessions.opened == 2
