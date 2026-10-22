"""Shared token buckets (`lorebase_store.ratelimit`) against Postgres."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_store import ratelimit

pytestmark = pytest.mark.integration

SLOW = 1e-6  # tokens per second: no visible refill during a test


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def take(sessions: async_sessionmaker[AsyncSession], lease: int, **spec: float) -> float:
    async with sessions() as session, session.begin():
        found = await ratelimit.lease(
            session,
            "key:k1|reads",
            capacity=int(spec.get("capacity", 10)),
            refill_per_s=spec.get("refill", SLOW),
            lease=lease,
        )
    return found.granted


async def test_leases_take_whole_tokens_until_the_bucket_is_empty(
    sessions: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    assert [await take(sessions, 4) for _ in range(4)] == [4, 4, 2, 0]
    async with sessions() as session:
        reading = await ratelimit.read(session, "key:k1|reads")
        assert reading.tokens is not None
        assert reading.tokens < 1
        empty = await ratelimit.read(session, "never-used")
        assert empty.tokens is None
    async with engine.connect() as connection:
        stored = (await connection.execute(text("SELECT key FROM rate_limit_buckets"))).scalars()
        assert list(stored) == [ratelimit.bucket_key("key:k1|reads")]  # names are not stored


async def test_concurrent_leases_never_exceed_the_bucket(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    granted = await asyncio.gather(*(take(sessions, 3, capacity=20) for _ in range(12)))
    assert sum(granted) == 20


async def test_buckets_refill_and_full_ones_are_pruned(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    assert await take(sessions, 5, capacity=5, refill=1000) == 5
    await asyncio.sleep(0.02)  # 1000 tokens per second: full again
    assert await take(sessions, 5, capacity=5, refill=1000) == 5
    await asyncio.sleep(0.02)
    async with sessions() as session, session.begin():
        assert await ratelimit.prune(session, limit=100) == 1
