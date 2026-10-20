from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from lorebase_platform import settings as platform_settings
from lorebase_store import migrations, testing
from lorebase_store.db import create_engine
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import ENV_ALIASES, DatabaseSettings, StoreSettings


@pytest.fixture(autouse=True)
def clean_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test without ambient LOREBASE__*, LOREBASE_CONFIG or alias variables."""
    for name in list(os.environ):
        if (
            name.startswith(platform_settings.ENV_PREFIX)
            or name == platform_settings.CONFIG_ENV_VAR
            or name in ENV_ALIASES
        ):
            monkeypatch.delenv(name)


@pytest.fixture(scope="session")
def postgres_server_url() -> Iterator[str]:
    """One Postgres server per test session (container or LOREBASE_TEST_DATABASE_URL)."""
    with testing.postgres_server() as url:
        yield url


@pytest.fixture
def store() -> StoreSettings:
    """`store` settings with code defaults."""
    return StoreSettings()


@pytest.fixture
def database() -> DatabaseSettings:
    """`database` settings with code defaults (the URL comes from the fixtures below)."""
    return DatabaseSettings()


@pytest.fixture
def empty_database_url(postgres_server_url: str) -> Iterator[str]:
    """A fresh empty database, dropped after the test."""
    with testing.scratch_database(postgres_server_url) as url:
        yield url


@pytest.fixture
def database_url(empty_database_url: str, database: DatabaseSettings, store: StoreSettings) -> str:
    """A fresh database migrated to head."""
    migrations.upgrade(database=database, store=store, url=empty_database_url)
    return empty_database_url


@pytest.fixture
async def engine(database_url: str, database: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    """Unpooled async engine on a freshly migrated database."""
    engine = create_engine(database, url=database_url, pooled=False)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def runtime(store: StoreSettings) -> StoreRuntime:
    """Store runtime with code defaults and its own node cache."""
    return StoreRuntime.from_settings(store)
