"""`read_session` against Postgres as a role subject to row-level security.

The same database is the primary and the "replica", which exercises the guard logic; a replica
that is behind is covered by the store tests.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from lorebase_core.revision import Author, Origin, Revision, RevisionStats
from lorebase_keeper.app import create_app
from lorebase_keeper.context import ReadSessionDep
from lorebase_platform.settings import Settings
from lorebase_store.db import create_engine
from lorebase_store.repositories import (
    DomainRepository,
    DomainRow,
    RefRepository,
    RevisionRepository,
    scope_to_domain,
)
from lorebase_store.settings import DatabaseSettings

pytestmark = pytest.mark.integration

D = "supply"
NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
OTHER_HEAD = hashlib.sha256(b"not a head").hexdigest()


def revision(parents: tuple[bytes, ...], label: str) -> Revision:
    return Revision(
        parents=parents,
        root=hashlib.sha256(label.encode()).digest(),
        domain=D,
        author=Author(principal="usr_t"),
        message=label,
        origin=Origin(type="test"),
        created_at=NOW,
        stats=RevisionStats(partitions_changed=0, changes=0),
    )


@pytest.fixture
async def head(database_url: str) -> bytes:
    """Seed the Domain `supply` with a `main` branch (as the database owner) and return its head."""
    engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            await DomainRepository(session).create(
                DomainRow(
                    id=D,
                    display_name="Supply",
                    description=None,
                    default_branch="main",
                    leaf_size=512,
                    settings={},
                    created_at=NOW,
                    created_by="usr_t",
                )
            )
            await scope_to_domain(session, D)
            row = await RevisionRepository(session, D).insert(revision((), "first"))
            await RefRepository(session, D).create(
                "main", row.id, created_at=NOW, created_by="usr_t"
            )
            return row.id
    finally:
        await engine.dispose()


def reads_app(dsn: str, *, fallback: bool = True, prefer: bool = True) -> FastAPI:
    database: dict[str, Any] = {
        "dsn": dsn,
        "read_dsn": [dsn],
        "read_fallback_to_primary": fallback,
    }
    app = create_app(
        Settings(
            keeper={"reads": {"prefer_replica": prefer}, "auth": {"mode": "none"}},
            database=database,
        )
    )
    router = APIRouter()

    @router.get("/probe/{domain_id}")
    async def probe(read: ReadSessionDep) -> dict[str, Any]:
        refs = await RefRepository(read.session, D).all()
        return {
            "source": read.route.source.value,
            "reason": read.route.reason.value,
            "refs": [r.name for r in refs],
        }

    app.include_router(router)
    return app


@asynccontextmanager
async def serving(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://keeper.test") as http:
            yield http


async def test_fresh_replica_serves_a_pinned_read_under_row_level_security(
    app_database_url: str, head: bytes
) -> None:
    async with serving(reads_app(app_database_url)) as http:
        response = await http.get(f"/probe/{D}", params={"min_revision": head.hex()})
    assert response.json() == {"source": "replica", "reason": "replica_fresh", "refs": ["main"]}


async def test_other_revision_is_served_by_the_primary(app_database_url: str, head: bytes) -> None:
    async with serving(reads_app(app_database_url)) as http:
        response = await http.get(f"/probe/{D}", params={"min_revision": OTHER_HEAD})
    assert response.json() == {"source": "primary", "reason": "stale", "refs": ["main"]}


async def test_other_revision_without_fallback_is_unavailable(
    app_database_url: str, head: bytes
) -> None:
    async with serving(reads_app(app_database_url, fallback=False)) as http:
        stale = await http.get(f"/probe/{D}", params={"min_revision": OTHER_HEAD})
        fresh = await http.get(f"/probe/{D}", params={"min_revision": head.hex()})
    assert stale.status_code == 503
    assert stale.json()["code"] == "unavailable"
    assert fresh.status_code == 200


async def test_replicas_are_ignored_unless_preferred(app_database_url: str, head: bytes) -> None:
    async with serving(reads_app(app_database_url, prefer=False)) as http:
        response = await http.get(f"/probe/{D}", params={"min_revision": head.hex()})
    assert response.json()["source"] == "primary"
    assert response.json()["reason"] == "not_preferred"


async def test_readyz_reports_the_replica(app_database_url: str, head: bytes) -> None:
    async with serving(reads_app(app_database_url)) as http:
        response = await http.get("/readyz")
    body = response.json()
    assert response.status_code == 200
    assert body["checks"]["database_replicas"]["status"] == "ok"
    assert "not a standby" in body["checks"]["database_replicas"]["detail"]
    assert "app-role-test-password" not in response.text
