from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_store.auth import SqlAuthStore
from lorebase_store.db import create_engine
from lorebase_store.settings import DatabaseSettings

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)

SeedDomain = Callable[[str], Awaitable[None]]


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """Unpooled engine on a migrated database, connected as the (superuser) test role."""
    engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def auth_store(sessions: async_sessionmaker[AsyncSession]) -> SqlAuthStore:
    return SqlAuthStore(sessions)


@pytest.fixture
def seed_domain(engine: AsyncEngine) -> SeedDomain:
    async def seed(domain_id: str) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO domains (id, display_name, leaf_size, created_at, created_by) "
                    "VALUES (:id, :id, 512, :at, 'usr_test')"
                ),
                {"id": domain_id, "at": NOW},
            )

    return seed
