"""Fixtures for tests that need Postgres (marked `integration`)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from lorebase_store import migrations, testing
from lorebase_store.definer import grant_read_functions
from lorebase_store.settings import DatabaseSettings, StoreSettings

APP_ROLE_PASSWORD = "app-role-test-password"


@pytest.fixture(scope="session")
def postgres_server_url() -> Iterator[str]:
    """One Postgres server per test session (container or LOREBASE_TEST_DATABASE_URL)."""
    with testing.postgres_server() as url:
        yield url


@pytest.fixture
def empty_database_url(postgres_server_url: str) -> Iterator[str]:
    """A fresh empty database, dropped after the test."""
    with testing.scratch_database(postgres_server_url) as url:
        yield url


@pytest.fixture
def database_url(empty_database_url: str) -> str:
    """A fresh database migrated to head."""
    migrations.upgrade(database=DatabaseSettings(), store=StoreSettings(), url=empty_database_url)
    return empty_database_url


async def _create_role(url: str, role: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text(f"CREATE ROLE {role} LOGIN PASSWORD '{APP_ROLE_PASSWORD}'")
            )
            for statement in (
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
                f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {role}",
            ):
                await connection.execute(text(statement))
            await grant_read_functions(connection, role)
    finally:
        await engine.dispose()


async def _drop_role(url: str, role: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f"DROP OWNED BY {role}"))
            await connection.execute(text(f"DROP ROLE {role}"))
    finally:
        await engine.dispose()


@pytest.fixture
def app_database_url(database_url: str, postgres_server_url: str) -> Iterator[str]:
    """URL of the migrated database for a role that is neither superuser nor BYPASSRLS.

    Keeper must work as such a role: row-level security applies to it.
    """
    role = f"lorebase_app_{uuid.uuid4().hex[:8]}"
    asyncio.run(_create_role(database_url, role))
    try:
        yield (
            make_url(database_url)
            .set(username=role, password=APP_ROLE_PASSWORD)
            .render_as_string(hide_password=False)
        )
    finally:
        asyncio.run(_drop_role(database_url, role))
