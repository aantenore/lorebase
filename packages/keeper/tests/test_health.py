from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from lorebase_keeper.app import create_app
from lorebase_keeper.readiness import CheckResult, DatabaseCheck, asyncpg_dsn
from lorebase_platform.settings import ConfigError, Settings

DSN = "postgresql+asyncpg://lorebase:s3cr3t-pass@db.internal:5432/lorebase"

ClientFor = Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]


class FakeConnection:
    def __init__(self, version: str | None, has_table: bool = True) -> None:
        self.version = version
        self.has_table = has_table
        self.closed = False

    async def fetchval(self, query: str) -> object:
        if "to_regclass" in query:
            return "alembic_version" if self.has_table else None
        return 1

    async def fetch(self, query: str) -> list[dict[str, str]]:
        return [{"version_num": self.version}] if self.version else []

    async def close(self) -> None:
        self.closed = True


def connector(connection: FakeConnection | Exception) -> Callable[[str, float], Awaitable[Any]]:
    async def connect(dsn: str, seconds: float) -> FakeConnection:
        if isinstance(connection, Exception):
            raise connection
        return connection

    return connect


def database_app(
    connection: FakeConnection | Exception, heads: Collection[str] | None = None
) -> FastAPI:
    check = DatabaseCheck(
        DSN,
        timeout_s=1,
        expected_heads=(lambda: heads) if heads is not None else None,
        connect=connector(connection),
    )
    return create_app(Settings(keeper={}, database={"dsn": DSN}), readiness_checks=[check])


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_without_database_section_reports_not_configured(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": {"status": "not_configured", "detail": None}},
    }


def test_database_check_is_built_from_settings(
    make_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_DB_DSN", DSN)
    app = make_app(sections={"database": {"dsn": "secret://env/TEST_DB_DSN"}})
    check = app.state.readiness_checks[0]
    assert isinstance(check, DatabaseCheck)


def test_database_section_name_is_configurable(make_app: Callable[..., FastAPI]) -> None:
    app = make_app({"readiness": {"database_section": "store"}}, sections={"store": {"dsn": DSN}})
    check = app.state.readiness_checks[0]
    assert isinstance(check, DatabaseCheck)


def test_unresolvable_database_secret_fails_at_startup(make_app: Callable[..., FastAPI]) -> None:
    with pytest.raises(ConfigError, match=r"database\.dsn"):
        make_app(sections={"database": {"dsn": "secret://env/LOREBASE_TEST_MISSING"}})


async def test_readyz_ready_when_database_answers_and_migrations_match(
    client_for: ClientFor,
) -> None:
    async with client_for(database_app(FakeConnection("abc123"), {"abc123"})) as http:
        response = await http.get("/readyz")
    assert response.status_code == 200
    assert response.json()["checks"]["database"]["status"] == "ok"


async def test_readyz_fails_when_database_is_unreachable(client_for: ClientFor) -> None:
    async with client_for(database_app(OSError(f"cannot connect to {DSN}"))) as http:
        response = await http.get("/readyz")
    body = response.json()
    assert response.status_code == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] == {"status": "failed", "detail": "OSError"}
    assert "s3cr3t-pass" not in response.text


async def test_readyz_fails_when_migrations_are_behind(client_for: ClientFor) -> None:
    async with client_for(database_app(FakeConnection("old000"), {"new111"})) as http:
        response = await http.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["database"]["detail"] == (
        "migrations at old000, expected new111"
    )


async def test_readyz_fails_when_migrations_were_never_applied(client_for: ClientFor) -> None:
    app = database_app(FakeConnection(None, has_table=False), {"new111"})
    async with client_for(app) as http:
        response = await http.get("/readyz")
    assert response.status_code == 503
    assert "not been applied" in response.json()["checks"]["database"]["detail"]


async def test_connection_is_closed_after_the_check() -> None:
    connection = FakeConnection("abc123")
    check = DatabaseCheck(
        DSN, timeout_s=1, expected_heads=lambda: ["abc123"], connect=connector(connection)
    )
    result = await check()
    assert result.status.value == "ok"
    assert connection.closed


def test_driver_suffix_is_removed_for_the_database_driver() -> None:
    assert asyncpg_dsn(DSN) == "postgresql://lorebase:s3cr3t-pass@db.internal:5432/lorebase"
    assert asyncpg_dsn("postgres://u@h/db") == "postgres://u@h/db"


async def test_slow_check_times_out(
    make_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    class Slow:
        name = "slow"

        async def __call__(self) -> CheckResult:
            await asyncio.sleep(5)
            raise AssertionError("unreachable")

    app = make_app({"readiness": {"timeout_s": 0.05}}, readiness_checks=[Slow()])
    async with client_for(app) as http:
        response = await http.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["slow"]["status"] == "failed"
