"""Async engine factory built from the `store` settings."""

from __future__ import annotations

from typing import Any

from sqlalchemy import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from lorebase_store.settings import DatabaseSettings

ASYNC_DRIVER = "postgresql+asyncpg"
_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql", ASYNC_DRIVER})


def async_url(url: str | URL) -> URL:
    """Parse a Postgres URL and switch it to the asyncpg driver."""
    parsed = make_url(url)
    if parsed.drivername not in _POSTGRES_SCHEMES:
        raise ValueError(f"unsupported database driver {parsed.drivername!r}; use postgresql://")
    return parsed.set(drivername=ASYNC_DRIVER)


def create_engine(
    database: DatabaseSettings,
    *,
    url: str | URL | None = None,
    pooled: bool = True,
    statement_timeout_ms: int | None = None,
) -> AsyncEngine:
    """Engine for `database`; `url` overrides the configured DSN, `pooled=False` uses no pool."""
    timeout = (
        database.statement_timeout_ms if statement_timeout_ms is None else statement_timeout_ms
    )
    connect_args = {
        "server_settings": {
            "application_name": database.application_name,
            "statement_timeout": str(timeout),
        }
    }
    pool_args: dict[str, Any] = (
        {
            "pool_size": database.pool_size,
            "max_overflow": database.max_overflow,
            "pool_timeout": database.pool_timeout_s,
            "pool_recycle": database.pool_recycle_s,
            "pool_pre_ping": True,
        }
        if pooled
        else {"poolclass": NullPool}
    )
    target = async_url(url if url is not None else database.dsn.get_secret_value())
    return create_async_engine(target, connect_args=connect_args, **pool_args)
