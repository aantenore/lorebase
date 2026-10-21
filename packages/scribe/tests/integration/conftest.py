"""Fixtures of the tests that need Postgres (marked `integration`)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_store import migrations, testing
from lorebase_store.db import create_engine
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings

from .support import Clock


@pytest.fixture(scope="session")
def postgres_server_url() -> Iterator[str]:
    """One Postgres server per test session (container or LOREBASE_TEST_DATABASE_URL)."""
    with testing.postgres_server() as url:
        yield url


@pytest.fixture
def database_url(postgres_server_url: str) -> Iterator[str]:
    """A fresh database migrated to head, dropped after the test."""
    with testing.scratch_database(postgres_server_url) as url:
        migrations.upgrade(database=DatabaseSettings(), store=StoreSettings(), url=url)
        yield url


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(DatabaseSettings(), url=database_url, statement_timeout_ms=0)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def runtime() -> StoreRuntime:
    return StoreRuntime.from_settings(StoreSettings())


@pytest.fixture
def clock() -> Clock:
    return Clock()
