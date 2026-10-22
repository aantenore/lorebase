"""Durable memory of structured output modes against Postgres."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_store.endpoint_modes import StoreModeMemory, clear_modes

pytestmark = pytest.mark.integration

DIGEST = bytes(range(32))
AGE = 3600.0


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def memory(sessions: async_sessionmaker[AsyncSession]) -> StoreModeMemory:
    return StoreModeMemory(sessions)


async def age(sessions: async_sessionmaker[AsyncSession], seconds: int) -> None:
    async with sessions.begin() as session:
        await session.execute(
            text(
                "UPDATE inference_endpoint_modes SET updated_at = now() - make_interval(secs => :s)"
            ),
            {"s": seconds},
        )


async def test_mode_is_remembered_and_only_moves_to_weaker_modes(memory: StoreModeMemory) -> None:
    assert await memory.recall(DIGEST, AGE) is None
    await memory.remember(DIGEST, 1, AGE)
    assert await memory.recall(DIGEST, AGE) == 1
    await memory.remember(DIGEST, 2, AGE)
    await memory.remember(DIGEST, 1, AGE)
    assert await memory.recall(DIGEST, AGE) == 2
    assert await memory.recall(bytes(32), AGE) is None


async def test_another_instance_reads_the_same_rows(
    memory: StoreModeMemory, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await memory.remember(DIGEST, 1, AGE)
    assert await StoreModeMemory(sessions).recall(DIGEST, AGE) == 1


async def test_old_entries_expire_and_are_replaced(
    memory: StoreModeMemory, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await memory.remember(DIGEST, 2, AGE)
    await age(sessions, 7200)
    assert await memory.recall(DIGEST, AGE) is None
    await memory.remember(DIGEST, 1, AGE)  # replaces the expired, stronger one
    assert await memory.recall(DIGEST, AGE) == 1


async def test_modes_can_be_cleared(
    memory: StoreModeMemory, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await memory.remember(DIGEST, 1, AGE)
    await memory.remember(bytes(32), 2, AGE)
    assert await clear_modes(sessions) == 2
    assert await memory.recall(DIGEST, AGE) is None
    assert await clear_modes(sessions) == 0


async def test_the_mode_index_is_bounded(
    memory: StoreModeMemory, sessions: async_sessionmaker[AsyncSession]
) -> None:
    with pytest.raises(Exception, match="mode_index"):
        await memory.remember(DIGEST, 3, AGE)


async def test_a_write_drops_the_other_expired_entries(
    memory: StoreModeMemory, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await memory.remember(bytes(32), 2, AGE)
    await age(sessions, 7200)
    await memory.remember(DIGEST, 1, AGE)
    async with sessions() as session:
        count = (
            await session.execute(text("SELECT count(*) FROM inference_endpoint_modes"))
        ).scalar()
    assert count == 1
