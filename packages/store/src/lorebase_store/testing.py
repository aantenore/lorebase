"""Postgres for tests: an existing server or a disposable container.

`postgres_server()` yields an asyncpg URL of a server where the role may create databases and
roles. It uses `LOREBASE_TEST_DATABASE_URL` when set; otherwise it starts a Postgres 16 +
pgvector container with testcontainers (image overridable with `LOREBASE_TEST_POSTGRES_IMAGE`).
`scratch_database()` creates an empty database on that server and drops it afterwards.
`nested_plans()` collects the plans of statements run inside functions (`auto_explain`).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
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


_AUTO_EXPLAIN = (
    "LOAD 'auto_explain'",
    "SET LOCAL auto_explain.log_min_duration = 0",
    "SET LOCAL auto_explain.log_nested_statements = on",
    "SET LOCAL auto_explain.log_format = json",
    "SET LOCAL client_min_messages = log",
)


@asynccontextmanager
async def nested_plans(session: AsyncSession) -> AsyncIterator[list[dict[str, Any]]]:
    """Plans (EXPLAIN JSON `Plan` nodes) of the statements the session's transaction runs
    meanwhile, including those run inside functions, as `auto_explain` reports them.

    Loading the module needs a superuser: enter before `SET LOCAL ROLE`. Raises
    `sqlalchemy.exc.DBAPIError` where the module cannot be loaded.
    """
    connection = await session.connection()
    for statement in _AUTO_EXPLAIN:
        await connection.exec_driver_sql(statement)
    driver = (await connection.get_raw_connection()).driver_connection
    assert driver is not None
    plans: list[dict[str, Any]] = []

    def listen(_connection: Any, message: Any) -> None:
        _, marker, body = str(message.message).partition("plan:\n")
        if marker:
            plans.append(json.loads(body)["Plan"])

    driver.add_log_listener(listen)
    try:
        yield plans
    finally:
        # A top-level statement's plan is logged when its portal closes, at the next statement.
        await connection.exec_driver_sql("SELECT 1")
        await asyncio.sleep(0)  # log messages are dispatched through the event loop
        driver.remove_log_listener(listen)
        await connection.exec_driver_sql("SET LOCAL client_min_messages = notice")


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
