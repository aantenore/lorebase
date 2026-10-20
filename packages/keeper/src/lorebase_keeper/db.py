"""Database engine and auth store of the application, built from the `database` section."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_platform.settings import Settings
from lorebase_store.auth import SqlAuthStore
from lorebase_store.db import create_engine
from lorebase_store.settings import DatabaseSettings


@dataclass(frozen=True)
class Database:
    """Engine, session factory and auth store sharing one connection pool."""

    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    auth_store: SqlAuthStore

    async def dispose(self) -> None:
        """Close the pool."""
        await self.engine.dispose()


def create_database(settings: Settings, section: str) -> Database | None:
    """The database named by `settings[section]`, or None when that section is absent.

    Connections are opened lazily, so building it does not require the server to be up.
    """
    if not (settings.model_extra or {}).get(section):
        return None
    engine = create_engine(settings.section(section, DatabaseSettings))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return Database(engine=engine, sessions=sessions, auth_store=SqlAuthStore(sessions))
