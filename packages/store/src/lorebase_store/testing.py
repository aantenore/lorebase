"""Postgres for tests: an existing server or a disposable container.

`postgres_server()` yields an asyncpg URL of a server where the role may create databases and
roles. It uses `LOREBASE_TEST_DATABASE_URL` when set; otherwise it starts a Postgres 16 +
pgvector container with testcontainers (image overridable with `LOREBASE_TEST_POSTGRES_IMAGE`).
`scratch_database()` creates an empty database on that server and drops it afterwards.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from lorebase_store.db import async_url

DATABASE_URL_ENV = "LOREBASE_TEST_DATABASE_URL"
IMAGE_ENV = "LOREBASE_TEST_POSTGRES_IMAGE"
DEFAULT_IMAGE = "pgvector/pgvector:pg16"
SCRATCH_PREFIX = "lorebase_test_"


@contextmanager
def postgres_server() -> Iterator[str]:
    """URL of a Postgres server for integration tests."""
    configured = os.environ.get(DATABASE_URL_ENV)
    if configured:
        yield async_url(configured).render_as_string(hide_password=False)
        return
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError as exc:
        raise RuntimeError(
            f"set {DATABASE_URL_ENV} or install the store dev dependencies "
            "(uv sync --all-packages) to start a Postgres container"
        ) from exc
    with PostgresContainer(os.environ.get(IMAGE_ENV, DEFAULT_IMAGE), driver="asyncpg") as pg:
        yield pg.get_connection_url()


async def _execute_autocommit(url: URL, statement: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


@contextmanager
def scratch_database(server_url: str) -> Iterator[str]:
    """Create a uniquely named empty database on `server_url`, yield its URL, then drop it."""
    server = async_url(server_url)
    name = f"{SCRATCH_PREFIX}{uuid.uuid4().hex[:12]}"
    asyncio.run(_execute_autocommit(server, f'CREATE DATABASE "{name}"'))
    try:
        yield server.set(database=name).render_as_string(hide_password=False)
    finally:
        asyncio.run(_execute_autocommit(server, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
