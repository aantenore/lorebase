"""`/readyz` against a real Postgres: it verifies the migrations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from lorebase_keeper.app import create_app
from lorebase_platform.settings import Settings
from lorebase_store.migrations import head_revisions

pytestmark = pytest.mark.integration


@asynccontextmanager
async def client_for(dsn: str) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(keeper={}, database={"dsn": dsn}))
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://keeper.test") as http:
        yield http


async def test_ready_after_migrations(database_url: str) -> None:
    async with client_for(database_url) as http:
        response = await http.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == {"status": "ok", "detail": None}
    assert body["checks"]["vector_search"]["status"] == "ok"
    assert body["checks"]["vector_search"]["detail"].startswith("pgvector ")


async def test_not_ready_before_migrations(empty_database_url: str) -> None:
    async with client_for(empty_database_url) as http:
        response = await http.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == {
        "status": "failed",
        "detail": "migrations have not been applied",
    }


async def test_not_ready_when_migrations_are_behind(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE alembic_version SET version_num = '0000_old'"))
    finally:
        await engine.dispose()
    async with client_for(database_url) as http:
        response = await http.get("/readyz")
    assert response.status_code == 503
    expected = ", ".join(sorted(head_revisions()))
    assert response.json()["checks"]["database"]["detail"] == (
        f"migrations at 0000_old, expected {expected}"
    )


async def _store_cursor(url: str, model: str) -> None:
    """An embedding cursor of `model`, as Scribe leaves one (foreign keys not enforced)."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL session_replication_role = replica"))
            await connection.execute(
                text(
                    "INSERT INTO embedding_cursors (domain_id, branch, model, head, fingerprints,"
                    " updated_at) VALUES ('supply', 'main', :model,"
                    " decode(repeat('00', 32), 'hex'), '{}', now())"
                ),
                {"model": model},
            )
    finally:
        await engine.dispose()


async def test_vector_search_is_degraded_when_no_vector_is_of_the_searched_model(
    database_url: str, app_database_url: str
) -> None:
    await _store_cursor(database_url, "other/model")
    # The application role is subject to row-level security: the check still sees the cursor.
    async with client_for(app_database_url) as http:
        response = await http.get("/readyz")
    assert response.status_code == 200
    vector = response.json()["checks"]["vector_search"]
    assert vector["status"] == "degraded"
    expected = "'BAAI/bge-small-en-v1.5' has no stored vectors (stored: other/model)"
    assert expected in vector["detail"]
    await _store_cursor(database_url, "BAAI/bge-small-en-v1.5")
    async with client_for(app_database_url) as http:
        response = await http.get("/readyz")
    assert response.json()["checks"]["vector_search"]["status"] == "ok"
