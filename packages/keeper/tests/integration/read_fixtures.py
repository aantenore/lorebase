"""A migrated database holding the `supply` example, shared by the read API tests of a module.

Seeding runs as the database owner; Keeper connects as a role without superuser or BYPASSRLS so
that row-level security applies to every read.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from lorebase_core.model import Schema
from lorebase_keeper.app import create_app
from lorebase_platform.settings import Settings
from lorebase_store import migrations, testing
from lorebase_store.db import create_engine
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings

from .supply import Clock

ROLE_PASSWORD = "read-role-test-password"


@dataclass
class Seeded:
    """What the seeding step produced, for assertions."""

    owner_url: str
    app_url: str
    schema: Schema
    revisions: dict[str, str] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)


async def _execute(url: str, *statements: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            for statement in statements:
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


@asynccontextmanager
async def owner_session(url: str) -> AsyncIterator[AsyncSession]:
    engine = create_engine(DatabaseSettings(), url=url, pooled=False)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


def seeded_database(server_url: str, seed: Any) -> Iterator[Seeded]:
    """Scratch database, migrated, seeded by `seed(session, runtime, clock, seeded)`."""
    with testing.scratch_database(server_url) as url:
        role = f"lorebase_read_{uuid.uuid4().hex[:8]}"
        asyncio.run(_execute(url, f"CREATE ROLE {role} LOGIN PASSWORD '{ROLE_PASSWORD}'"))
        # As in production: the role exists first and the migration grants it the read
        # functions (database.application_role).
        migrations.upgrade(
            database=DatabaseSettings(application_role=role), store=StoreSettings(), url=url
        )
        asyncio.run(
            _execute(
                url,
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
                f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {role}",
            )
        )
        app_url = (
            make_url(url)
            .set(username=role, password=ROLE_PASSWORD)
            .render_as_string(hide_password=False)
        )

        async def run() -> Seeded:
            async with owner_session(url) as session:
                return await seed(
                    session, StoreRuntime.from_settings(StoreSettings()), Clock(), url, app_url
                )

        try:
            yield asyncio.run(run())
        finally:
            asyncio.run(_execute(url, f"DROP OWNED BY {role}", f"DROP ROLE {role}"))


@asynccontextmanager
async def keeper(
    app_url: str, keeper_section: dict[str, Any] | None = None, **sections: Any
) -> AsyncIterator[httpx.AsyncClient]:
    """A Keeper application on `app_url` (default auth mode none) and an HTTP client for it."""
    settings = Settings(keeper=keeper_section or {}, database={"dsn": app_url}, **sections)
    app: FastAPI = create_app(settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://keeper.test") as http,
    ):
        yield http
