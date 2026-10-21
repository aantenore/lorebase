"""Rate limits through the application: headers, 429 problem details, exemptions, audit."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from lorebase_platform.telemetry import Telemetry
from lorebase_store.models.enums import PrincipalType

PEPPER = "test-pepper-0123456789-0123456789-abcdef"
API_KEYS: dict[str, Any] = {"mode": "api_key", "api_keys": {"pepper": PEPPER}}
WHOAMI = "/api/v1/auth/whoami"
SLOW = 0.001  # requests per minute: nothing refills during a test

ClientFor = Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]
AppFactory = Callable[..., FastAPI]


def rate(**classes: dict[str, float]) -> dict[str, Any]:
    return {"limits": {"rate": classes}}


def bucket(burst: int) -> dict[str, float]:
    return {"requests_per_minute": SLOW, "burst": burst}


async def test_every_response_carries_the_rate_limit_headers(
    make_app: AppFactory, client_for: ClientFor
) -> None:
    app = make_app(rate(reads={"requests_per_minute": 60, "burst": 10}))
    async with client_for(app) as http:
        first = await http.get(WHOAMI)
        second = await http.get(WHOAMI)
    assert first.status_code == 200
    assert first.headers["ratelimit-limit"] == "10"
    assert first.headers["ratelimit-remaining"] == "9"
    assert int(first.headers["ratelimit-reset"]) >= 1
    assert second.headers["ratelimit-remaining"] == "8"
    assert "retry-after" not in second.headers


async def test_a_spent_bucket_answers_429_with_problem_details_and_retry_after(
    make_app: AppFactory, client_for: ClientFor
) -> None:
    app = make_app(rate(reads={"requests_per_minute": 60, "burst": 2}))
    async with client_for(app) as http:
        statuses = [(await http.get(WHOAMI)).status_code for _ in range(2)]
        refused = await http.get(WHOAMI)
    assert statuses == [200, 200]
    assert refused.status_code == 429
    assert refused.headers["content-type"] == "application/problem+json"
    assert refused.headers["retry-after"] == "1"
    assert refused.headers["ratelimit-limit"] == "2"
    assert refused.headers["ratelimit-remaining"] == "0"
    assert refused.headers["x-request-id"]
    body = refused.json()
    assert body["type"] == "urn:lorebase:problem:rate-limited"
    assert body["code"] == "rate-limited"
    assert body["status"] == 429
    assert body["class"] == "reads"
    assert body["retry_after_s"] == 1
    assert body["request_id"] == refused.headers["x-request-id"]


async def test_the_retry_after_matches_the_refill_rate(
    make_app: AppFactory, client_for: ClientFor
) -> None:
    app = make_app(rate(reads={"requests_per_minute": 6, "burst": 1}))
    async with client_for(app) as http:
        await http.get(WHOAMI)
        refused = await http.get(WHOAMI)
    assert refused.headers["retry-after"] == "10"
    assert refused.headers["ratelimit-reset"] == "10"


async def test_health_endpoints_are_exempt(make_app: AppFactory, client_for: ClientFor) -> None:
    app = make_app(rate(reads=bucket(1)))
    async with client_for(app) as http:
        health = [await http.get("/healthz") for _ in range(5)]
        ready = [await http.get("/readyz") for _ in range(5)]
        api = [await http.get(WHOAMI) for _ in range(2)]
    assert [r.status_code for r in health] == [200] * 5
    assert 429 not in {r.status_code for r in ready}
    assert all("ratelimit-limit" not in r.headers for r in [*health, *ready])
    assert [r.status_code for r in api] == [200, 429]  # health requests took nothing


async def test_exempt_paths_are_configurable(make_app: AppFactory, client_for: ClientFor) -> None:
    app = make_app({"limits": {"rate": {"reads": bucket(1), "exempt_paths": ["/auth/whoami"]}}})
    async with client_for(app) as http:
        statuses = [(await http.get(WHOAMI)).status_code for _ in range(3)]
    assert statuses == [200, 200, 200]


async def test_rate_limiting_can_be_turned_off(make_app: AppFactory, client_for: ClientFor) -> None:
    app = make_app({"limits": {"rate": {"enabled": False, "reads": bucket(1)}}})
    async with client_for(app) as http:
        responses = [await http.get(WHOAMI) for _ in range(3)]
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert all("ratelimit-limit" not in r.headers for r in responses)


async def test_classes_have_their_own_buckets(make_app: AppFactory, client_for: ClientFor) -> None:
    app = make_app(rate(reads=bucket(5), writes=bucket(1)))
    write = "/api/v1/domains"  # fails later for lack of a database, but it is a write
    async with client_for(app) as http:
        first = await http.post(write, json={"id": "supply"})
        second = await http.post(write, json={"id": "supply"})
        read = await http.get(WHOAMI)
    assert first.status_code != 429
    assert second.status_code == 429
    assert second.json()["class"] == "writes"
    assert read.status_code == 200


async def test_each_principal_has_its_own_bucket(
    make_app: AppFactory, client_for: ClientFor, store: Any, codec: Any, clock: Any
) -> None:
    store.add_principal("svc_one", PrincipalType.SERVICE)
    store.add_principal("svc_two", PrincipalType.SERVICE)
    one, _ = store.add_key(codec, "svc_one")
    two, _ = store.add_key(codec, "svc_two")
    app = make_app(
        {"auth": API_KEYS, "limits": {"rate": {"reads": bucket(2)}}},
        auth_store=store,
        clock=clock,
    )

    def as_key(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async with client_for(app) as http:
        spent = [(await http.get(WHOAMI, headers=as_key(one))).status_code for _ in range(3)]
        other = (await http.get(WHOAMI, headers=as_key(two))).status_code
        unauthenticated = (await http.get(WHOAMI)).status_code
    assert spent == [200, 200, 429]
    assert other == 200
    assert unauthenticated == 401  # no principal, no bucket


async def test_domain_overrides_apply_to_that_domain_only(
    make_app: AppFactory, client_for: ClientFor
) -> None:
    app = make_app(
        {
            "limits": {
                "rate": {
                    "reads": {"requests_per_minute": SLOW, "burst": 50},
                    "domains": {"acme": {"reads": bucket(1)}},
                }
            }
        }
    )
    async with client_for(app) as http:
        acme = [(await http.get("/api/v1/domains/acme/schema")).status_code for _ in range(2)]
        globex = [(await http.get("/api/v1/domains/globex/schema")).status_code for _ in range(3)]
    assert acme[1] == 429
    assert acme[0] != 429
    assert 429 not in globex


async def test_refusals_are_counted_by_class(make_app: AppFactory, client_for: ClientFor) -> None:
    reader = InMemoryMetricReader()
    telemetry = Telemetry(enabled=True, meter_provider=MeterProvider(metric_readers=[reader]))
    app = make_app(rate(reads=bucket(1)), telemetry=telemetry)
    async with client_for(app) as http:
        for _ in range(4):
            await http.get(WHOAMI)
    counted = {}
    for resource in reader.get_metrics_data().resource_metrics:  # type: ignore[union-attr]
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == "lorebase.http.rate_limited":
                    for point in metric.data.data_points:
                        counted[point.attributes["class"]] = point.value
    assert counted == {"reads": 3}


async def test_repeated_throttling_writes_one_audit_entry_per_threshold(
    make_app: AppFactory, client_for: ClientFor, store: Any, codec: Any, clock: Any
) -> None:
    store.add_principal("svc_noisy", PrincipalType.SERVICE)
    token, key = store.add_key(codec, "svc_noisy")
    store.add_grant("acme", "svc_noisy", "admin")
    app = make_app(
        {
            "auth": API_KEYS,
            "limits": {"rate": {"admin": bucket(1), "throttle_audit": {"threshold": 3}}},
        },
        auth_store=store,
        clock=clock,
    )
    headers = {"Authorization": f"Bearer {token}"}
    async with client_for(app) as http:
        statuses = [
            (
                await http.post(
                    "/api/v1/domains/acme/api-keys", json={"name": "b"}, headers=headers
                )
            ).status_code
            for _ in range(8)
        ]
    assert statuses[0] == 201
    assert set(statuses[1:]) == {429}
    reported = [e for e in store.audit_rows if e.action == "rate_limit.throttled"]
    assert len(reported) == 2  # refusals 3 and 6 of the seven
    first = reported[0]
    assert first.actor == "svc_noisy"
    assert first.domain_id == "acme"
    assert first.target == key.id
    assert first.details["refusals"] == 3
    assert first.details["reason"] == "admin"
    # the throttled requests themselves are not logged as mutations: only the one that ran
    assert [e.action for e in store.audit_rows if e.action != "rate_limit.throttled"] == [
        "apikey.create"
    ]


async def test_throttle_audit_can_be_turned_off(
    make_app: AppFactory, client_for: ClientFor, store: Any, codec: Any, clock: Any
) -> None:
    store.add_principal("svc_noisy", PrincipalType.SERVICE)
    token, _ = store.add_key(codec, "svc_noisy")
    app = make_app(
        {
            "auth": API_KEYS,
            "limits": {"rate": {"reads": bucket(1), "throttle_audit": {"threshold": 0}}},
        },
        auth_store=store,
        clock=clock,
    )
    async with client_for(app) as http:
        for _ in range(10):
            await http.get(WHOAMI, headers={"Authorization": f"Bearer {token}"})
    assert store.audit_rows == []


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
async def test_health_is_reachable_without_credentials_when_authentication_is_on(
    make_app: AppFactory, client_for: ClientFor, store: Any, path: str
) -> None:
    app = make_app({"auth": API_KEYS}, auth_store=store)
    async with client_for(app) as http:
        response = await http.get(path)
    assert response.status_code != 401
