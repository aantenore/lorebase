"""Fixtures that run a real Keeper on Postgres for the client and CLI integration tests.

Registered as a plugin from the root `conftest.py`. Nothing heavy is imported at load time:
Keeper, the store and the database driver are imported inside the fixtures, so unit runs
that never ask for them stay fast and need no services.

- `keeper_database`: a fresh migrated database, an application role without superuser rights
  (row-level security applies to it) and an installation administrator with an API key.
- `in_process_keeper`: the application served through `httpx.ASGITransport`.
- `served_keeper`: the application served on a local port by uvicorn in a thread, for code
  that makes real HTTP requests with a blocking client.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

PEPPER = "harness-pepper-0123456789-0123456789-abcdef"
APP_ROLE_PASSWORD = "app-role-test-password"
IN_PROCESS_URL = "http://keeper.test"
START_TIMEOUT_S = 30.0
START_POLL_S = 0.05


@dataclass(frozen=True)
class KeeperDatabase:
    """A migrated database reachable as the application role, and its administrator."""

    app_url: str
    admin_id: str
    admin_key: str


@dataclass(frozen=True)
class RunningKeeper:
    """A Keeper to talk to."""

    url: str
    admin_key: str
    app: Any
    transport: httpx.ASGITransport | None = None


async def _execute(url: str, *statements: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            for statement in statements:
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def _create_admin(database_url: str) -> tuple[str, str]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from lorebase_keeper.auth.api_keys import ApiKeyCodec
    from lorebase_keeper.auth.config import ApiKeySettings
    from lorebase_store.auth import SqlAuthStore
    from lorebase_store.db import create_engine
    from lorebase_store.models.enums import PrincipalType
    from lorebase_store.settings import DatabaseSettings

    engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    codec = ApiKeyCodec(ApiKeySettings(pepper=PEPPER))
    try:
        store = SqlAuthStore(async_sessionmaker(engine, expire_on_commit=False))
        async with store.unit() as unit:
            admin = await unit.principals.add(PrincipalType.SERVICE, "installation admin")
            generated = codec.generate()
            await unit.api_keys.add(
                principal_id=admin.id, prefix=generated.prefix, key_hash=generated.key_hash
            )
    finally:
        await engine.dispose()
    return admin.id, generated.token


@pytest.fixture(scope="session")
def keeper_postgres_url() -> Iterator[str]:
    """One Postgres server per session (container, or LOREBASE_TEST_DATABASE_URL)."""
    from lorebase_store import testing

    with testing.postgres_server() as url:
        yield url


@pytest.fixture
def keeper_database(keeper_postgres_url: str) -> Iterator[KeeperDatabase]:
    """A fresh database migrated to head, an application role and an administrator key."""
    from sqlalchemy import make_url

    from lorebase_store import migrations, testing
    from lorebase_store.settings import DatabaseSettings, StoreSettings

    with testing.scratch_database(keeper_postgres_url) as url:
        migrations.upgrade(database=DatabaseSettings(), store=StoreSettings(), url=url)
        role = f"lorebase_app_{uuid.uuid4().hex[:8]}"
        asyncio.run(
            _execute(
                url,
                f"CREATE ROLE {role} LOGIN PASSWORD '{APP_ROLE_PASSWORD}'",
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
                f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {role}",
            )
        )
        admin_id, admin_key = asyncio.run(_create_admin(url))
        app_url = (
            make_url(url)
            .set(username=role, password=APP_ROLE_PASSWORD)
            .render_as_string(hide_password=False)
        )
        try:
            yield KeeperDatabase(app_url, admin_id, admin_key)
        finally:
            asyncio.run(_execute(url, f"DROP OWNED BY {role}", f"DROP ROLE {role}"))


def build_app(database: KeeperDatabase, **keeper_settings: Any) -> Any:
    """The Keeper application with API key authentication on the given database."""
    from lorebase_keeper.app import create_app
    from lorebase_platform.settings import Settings

    settings = Settings(
        keeper={
            "auth": {
                "mode": "api_key",
                "api_keys": {"pepper": PEPPER},
                "installation_admins": [database.admin_id],
                "authz": {"grants_cache_ttl_s": 0},
            },
            **keeper_settings,
        },
        database={"dsn": database.app_url},
    )
    return create_app(settings)


@pytest.fixture
async def in_process_keeper(keeper_database: KeeperDatabase) -> AsyncIterator[RunningKeeper]:
    """The application called in-process (give `transport` to an async client)."""
    app = build_app(keeper_database)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with app.router.lifespan_context(app):
        yield RunningKeeper(IN_PROCESS_URL, keeper_database.admin_key, app, transport)


@pytest.fixture
def served_keeper(keeper_database: KeeperDatabase) -> Iterator[RunningKeeper]:
    """The application served on 127.0.0.1 by uvicorn in a background thread."""
    import uvicorn

    app = build_app(keeper_database)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    waited = 0.0
    while not server.started:
        if not thread.is_alive() or waited > START_TIMEOUT_S:
            raise RuntimeError("the test server did not start")
        threading.Event().wait(START_POLL_S)
        waited += START_POLL_S
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield RunningKeeper(f"http://127.0.0.1:{port}", keeper_database.admin_key, app)
    finally:
        server.should_exit = True
        thread.join(timeout=START_TIMEOUT_S)
