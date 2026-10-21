"""Daily quotas of API keys: counting, enforcement, persistence and the whoami report."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import date
from typing import Any

import httpx
from fastapi import FastAPI

from lorebase_keeper.ratelimit.config import QuotaSettings
from lorebase_keeper.ratelimit.quotas import QuotaTracker
from lorebase_store.models.enums import PrincipalType

PEPPER = "test-pepper-0123456789-0123456789-abcdef"
API_KEYS: dict[str, Any] = {"mode": "api_key", "api_keys": {"pepper": PEPPER}}
WHOAMI = "/api/v1/auth/whoami"
TODAY = date(2026, 10, 20)

ClientFor = Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]
AppFactory = Callable[..., FastAPI]


class Ticker:
    """A monotonic clock moved by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def tracker(store: Any, clock: Any, ticker: Ticker | None = None, **settings: Any) -> QuotaTracker:
    return QuotaTracker(QuotaSettings(**settings), store, clock=clock, monotonic=ticker or Ticker())


def keyed_app(make_app: AppFactory, store: Any, clock: Any, quota: dict[str, Any]) -> FastAPI:
    return make_app({"auth": API_KEYS, "limits": {"quota": quota}}, auth_store=store, clock=clock)


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_requests_are_counted_and_refused_once_the_quota_is_used_up(
    store: Any, clock: Any
) -> None:
    quotas = tracker(store, clock, requests_per_day=3)
    outcomes = [(await quotas.admit("key_1"))[0] for _ in range(5)]
    assert outcomes == [None, None, None, "requests", "requests"]
    _, snapshot = await quotas.admit("key_1")
    assert snapshot.requests_used == 3  # refusals are not counted
    assert snapshot.resets_at.isoformat() == "2026-10-21T00:00:00+00:00"


async def test_context_tokens_have_a_quota_of_their_own(store: Any, clock: Any) -> None:
    quotas = tracker(store, clock, context_tokens_per_day=100)
    assert await quotas.check_tokens("key_1") is None
    await quotas.charge_tokens("key_1", 60)
    assert await quotas.check_tokens("key_1") is None
    await quotas.charge_tokens("key_1", 60)  # a pack that started under the limit is whole
    refused = await quotas.check_tokens("key_1")
    assert refused is not None
    assert (refused.tokens_used, refused.tokens_limit) == (120, 100)
    assert (await quotas.admit("key_1"))[0] == "context_tokens"


async def test_use_is_written_at_most_once_per_interval_and_on_close(
    store: Any, clock: Any
) -> None:
    ticker = Ticker()
    quotas = tracker(store, clock, ticker, flush_interval_s=10)
    for _ in range(4):
        await quotas.admit("key_1")
    assert store.usage_writes == 1  # the first request wrote at once, the rest wait
    assert store.usage_rows[("key_1", TODAY)].requests == 1
    ticker.now = 11
    await quotas.admit("key_1")
    assert store.usage_rows[("key_1", TODAY)].requests == 5
    await quotas.admit("key_1")
    await quotas.charge_tokens("key_1", 7)
    await quotas.aclose()
    row = store.usage_rows[("key_1", TODAY)]
    assert (row.requests, row.context_tokens) == (6, 7)


async def test_processes_sharing_a_store_see_each_others_use(store: Any, clock: Any) -> None:
    one = tracker(store, clock, requests_per_day=10, flush_interval_s=1)
    two = tracker(store, clock, requests_per_day=10, flush_interval_s=1)
    for _ in range(4):
        await one.admit("key_1")
    await one.aclose()
    snapshot = await two.snapshot("key_1")
    assert snapshot.requests_used == 4
    await two.admit("key_1")
    await two.aclose()
    assert (await one.snapshot("key_1")).requests_used == 5


async def test_a_quota_survives_a_restart(store: Any, clock: Any) -> None:
    before = tracker(store, clock, requests_per_day=2)
    await before.admit("key_1")
    await before.admit("key_1")
    await before.aclose()
    after = tracker(store, clock, requests_per_day=2)
    assert (await after.admit("key_1"))[0] == "requests"


async def test_counters_reset_at_midnight_utc(store: Any, clock: Any) -> None:
    quotas = tracker(store, clock, requests_per_day=1)
    assert (await quotas.admit("key_1"))[0] is None
    assert (await quotas.admit("key_1"))[0] == "requests"
    clock.advance(hours=15)  # 09:00 + 15 h: the next day
    refused, snapshot = await quotas.admit("key_1")
    assert refused is None
    assert snapshot.day == date(2026, 10, 21)
    assert store.usage_rows[("key_1", TODAY)].requests == 1  # yesterday's use stays on its day


async def test_a_store_failure_never_refuses_a_request(store: Any, clock: Any) -> None:
    store.usage_failure = RuntimeError("database down")
    quotas = tracker(store, clock, requests_per_day=2)
    outcomes = [(await quotas.admit("key_1"))[0] for _ in range(3)]
    assert outcomes == [None, None, "requests"]  # enforced on what is known
    store.usage_failure = None
    await quotas.aclose()
    assert store.usage_rows[("key_1", TODAY)].requests == 2  # nothing was lost


async def test_the_quota_refuses_with_429_and_the_time_to_the_reset(
    make_app: AppFactory, client_for: ClientFor, store: Any, codec: Any, clock: Any
) -> None:
    store.add_principal("svc_bot", PrincipalType.SERVICE)
    token, _ = store.add_key(codec, "svc_bot")
    app = keyed_app(make_app, store, clock, {"requests_per_day": 2})
    async with client_for(app) as http:
        statuses = [(await http.get(WHOAMI, headers=headers(token))).status_code for _ in range(2)]
        refused = await http.get(WHOAMI, headers=headers(token))
    assert statuses == [200, 200]
    assert refused.status_code == 429
    assert refused.headers["content-type"] == "application/problem+json"
    assert refused.headers["retry-after"] == str(15 * 3600)  # 09:00 to midnight
    body = refused.json()
    assert body["code"] == "quota-exceeded"
    assert body["type"] == "urn:lorebase:problem:quota-exceeded"
    assert body["quota"] == "requests"
    assert body["limit"] == 2
    assert body["resets_at"] == "2026-10-21T00:00:00+00:00"


async def test_whoami_reports_the_quota_of_the_key(
    make_app: AppFactory, client_for: ClientFor, store: Any, codec: Any, clock: Any
) -> None:
    store.add_principal("svc_bot", PrincipalType.SERVICE)
    token, key = store.add_key(codec, "svc_bot")
    app = keyed_app(
        make_app,
        store,
        clock,
        {
            "requests_per_day": 100,
            "context_tokens_per_day": 500,
            "keys": {key.id: {"requests_per_day": 10}},
        },
    )
    async with client_for(app) as http:
        await http.get(WHOAMI, headers=headers(token))
        report = (await http.get(WHOAMI, headers=headers(token))).json()["quota"]
    assert report == {
        "period": "day",
        "resets_at": "2026-10-21T00:00:00Z",
        "requests": {"limit": 10, "used": 2, "remaining": 8},
        "context_tokens": {"limit": 500, "used": 0, "remaining": 500},
    }


async def test_a_key_can_be_exempted_and_quotas_can_be_turned_off(
    make_app: AppFactory, client_for: ClientFor, store: Any, codec: Any, clock: Any
) -> None:
    store.add_principal("svc_bot", PrincipalType.SERVICE)
    token, key = store.add_key(codec, "svc_bot")
    unlimited = {"requests_per_day": 1, "keys": {key.id: {"requests_per_day": None}}}
    async with client_for(keyed_app(make_app, store, clock, unlimited)) as http:
        statuses = [(await http.get(WHOAMI, headers=headers(token))).status_code for _ in range(3)]
        report = (await http.get(WHOAMI, headers=headers(token))).json()["quota"]
    assert statuses == [200, 200, 200]
    assert report["requests"]["limit"] is None
    assert report["requests"]["remaining"] is None

    reads, writes = store.usage_reads, store.usage_writes
    off = keyed_app(make_app, store, clock, {"enabled": False, "requests_per_day": 1})
    async with client_for(off) as http:
        answers = [(await http.get(WHOAMI, headers=headers(token))).json() for _ in range(3)]
    assert all(answer["quota"] is None for answer in answers)
    assert (store.usage_reads, store.usage_writes) == (reads, writes)
