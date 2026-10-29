"""Readiness compares the database revision with the heads of the store's migrations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
from fastapi import FastAPI

from lorebase_keeper.app import create_app
from lorebase_keeper.readiness import DatabaseCheck, packaged_heads
from lorebase_platform.settings import Settings
from lorebase_store.migrations import head_revisions

DSN = "postgresql+asyncpg://lorebase:s3cr3t-pass@db.internal:5432/lorebase"

ClientFor = Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]


class Connection:
    def __init__(self, revisions: list[str]) -> None:
        self.revisions = revisions

    async def fetchval(self, query: str) -> object:
        return "alembic_version" if "to_regclass" in query else 1

    async def fetch(self, query: str) -> list[dict[str, str]]:
        return [{"version_num": revision} for revision in self.revisions]

    async def close(self) -> None:
        return None


def connector(revisions: list[str]) -> Callable[[str, float], Awaitable[Any]]:
    async def connect(dsn: str, seconds: float) -> Connection:
        return Connection(revisions)

    return connect


def default_check(app: FastAPI) -> DatabaseCheck:
    check = app.state.readiness_checks[0]
    assert isinstance(check, DatabaseCheck)
    return check


def test_the_store_heads_are_known_and_read_once() -> None:
    assert packaged_heads() == head_revisions()
    assert packaged_heads()  # a code base without migrations would never be ready
    assert packaged_heads() is packaged_heads()


def test_the_default_check_expects_the_store_heads() -> None:
    app = create_app(Settings(keeper={}, database={"dsn": DSN}))
    assert default_check(app).expected_heads == frozenset(head_revisions())


def test_heads_can_be_overridden_or_skipped() -> None:
    settings = Settings(keeper={}, database={"dsn": DSN})
    assert default_check(create_app(settings, migration_heads=lambda: ["x1"])).expected_heads == {
        "x1"
    }
    assert default_check(create_app(settings, migration_heads=tuple)).expected_heads == frozenset()


async def test_ready_when_the_database_is_at_the_store_heads(client_for: ClientFor) -> None:
    check = DatabaseCheck(
        DSN,
        timeout_s=1,
        expected_heads=packaged_heads,
        connect=connector(list(head_revisions())),
    )
    app = create_app(Settings(keeper={}), readiness_checks=[check])
    async with client_for(app) as http:
        response = await http.get("/readyz")
    assert response.status_code == 200
    assert response.json()["checks"]["database"]["status"] == "ok"


async def test_not_ready_when_the_database_is_behind_the_store(client_for: ClientFor) -> None:
    check = DatabaseCheck(
        DSN, timeout_s=1, expected_heads=packaged_heads, connect=connector(["0000_old"])
    )
    app = create_app(Settings(keeper={}), readiness_checks=[check])
    async with client_for(app) as http:
        response = await http.get("/readyz")
    assert response.status_code == 503
    detail = response.json()["checks"]["database"]["detail"]
    assert detail.startswith("migrations at 0000_old, expected ")
    assert "s3cr3t-pass" not in response.text
