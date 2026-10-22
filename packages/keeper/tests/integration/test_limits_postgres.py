"""Rate limits and daily quotas against a real Postgres, as a non-superuser role."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from lorebase_keeper.app import create_app
from lorebase_platform.settings import Settings
from lorebase_store.db import create_engine
from lorebase_store.settings import DatabaseSettings

from .test_auth_postgres import BASE, PEPPER, seed

pytestmark = pytest.mark.integration

WHOAMI = "/api/v1/auth/whoami"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
SLOW = 0.001


@asynccontextmanager
async def running(
    app_database_url: str, limits: dict[str, Any]
) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        keeper={"auth": {"mode": "api_key", "api_keys": {"pepper": PEPPER}}, "limits": limits},
        database={"dsn": app_database_url},
    )
    app: FastAPI = create_app(settings, clock=lambda: NOW)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://keeper.test") as http,
    ):
        yield http


async def stored_usage(database_url: str) -> list[tuple[date, int, int]]:
    engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT day, requests, context_tokens FROM api_key_usage ORDER BY day")
            )
            return [(day, int(requests), int(tokens)) for day, requests, tokens in rows]
    finally:
        await engine.dispose()


async def test_rate_limits_and_quotas_hold_across_restarts(
    database_url: str, app_database_url: str
) -> None:
    key = await seed(database_url)
    headers = {"Authorization": f"Bearer {key}"}
    limits: dict[str, Any] = {
        "rate": {"admin": {"requests_per_minute": SLOW, "burst": 3}},
        "quota": {"requests_per_day": 5, "context_tokens_per_day": 1000},
    }

    async with running(app_database_url, limits) as http:
        grants = [await http.get(f"{BASE}/grants", headers=headers) for _ in range(3)]
        assert [r.status_code for r in grants] == [200, 200, 200]
        assert grants[0].headers["ratelimit-limit"] == "3"
        assert grants[2].headers["ratelimit-remaining"] == "0"

        refused = await http.get(f"{BASE}/grants", headers=headers)
        assert refused.status_code == 429
        assert refused.json()["code"] == "rate-limited"
        assert int(refused.headers["retry-after"]) >= 1

        me = (await http.get(WHOAMI, headers=headers)).json()
        assert me["quota"] == {
            "period": "day",
            "resets_at": "2026-10-21T00:00:00Z",
            "requests": {"limit": 5, "used": 4, "remaining": 1},  # the refused one is not counted
            "context_tokens": {"limit": 1000, "used": 0, "remaining": 1000},
        }

    assert await stored_usage(database_url) == [(date(2026, 10, 20), 4, 0)]

    # a new process continues from the stored total
    async with running(app_database_url, limits) as http:
        assert (await http.get(WHOAMI, headers=headers)).status_code == 200  # the fifth
        over = await http.get(WHOAMI, headers=headers)
        assert over.status_code == 429
        assert over.json()["code"] == "quota-exceeded"
        assert over.headers["retry-after"] == str(12 * 3600)

    assert await stored_usage(database_url) == [(date(2026, 10, 20), 5, 0)]


async def test_two_keepers_share_their_buckets(database_url: str, app_database_url: str) -> None:
    key = await seed(database_url)
    headers = {"Authorization": f"Bearer {key}"}
    limits: dict[str, Any] = {
        "rate": {
            "backend": "postgres",
            "admin": {"requests_per_minute": SLOW, "burst": 8},
            "auth_failures": {"requests_per_minute": SLOW, "burst": 3},
            "postgres": {"lease_tokens": 2, "check_cache_s": 0},
        },
        "quota": {"enabled": False},
    }
    async with (
        running(app_database_url, limits) as one,
        running(app_database_url, limits) as two,
    ):
        keepers = (one, two)
        statuses = [
            (await keepers[n % 2].get(f"{BASE}/grants", headers=headers)).status_code
            for n in range(12)
        ]
        # each Keeper alone would allow 8; together they share one bucket of 8
        assert statuses == [200] * 8 + [429] * 4

        wrong = {"Authorization": f"Bearer {key[:-4]}abcd"}
        failures = [(await keepers[n % 2].get(WHOAMI, headers=wrong)).status_code for n in range(5)]
        assert failures == [401, 401, 401, 429, 429]

    engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    try:
        async with engine.connect() as connection:
            stored = await connection.execute(text("SELECT count(*) FROM rate_limit_buckets"))
            assert stored.scalar_one() >= 2  # the admin bucket and the failure buckets
    finally:
        await engine.dispose()
