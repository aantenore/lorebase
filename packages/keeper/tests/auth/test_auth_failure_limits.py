"""Failed authentications are limited per client address and per API key prefix."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_store.models.enums import PrincipalType

PEPPER = "test-pepper-0123456789-0123456789-abcdef"
ISSUER = "https://idp.example.test"
AUDIENCE = "lorebase-api"
WHOAMI = "/api/v1/auth/whoami"
SLOW = 0.001  # requests per minute: nothing refills during a test
BURST = 3

AppFactory = Callable[..., FastAPI]


def auth_settings(mode: str = "api_key", **failures: Any) -> dict[str, Any]:
    auth: dict[str, Any] = {"mode": mode}
    if "api_key" in mode:
        auth["api_keys"] = {"pepper": PEPPER}
    if "oidc" in mode:
        auth["oidc"] = {"issuer": ISSUER, "audience": AUDIENCE}
    bucket = {"requests_per_minute": SLOW, "burst": BURST, **failures}
    return {"auth": auth, "limits": {"rate": {"auth_failures": bucket}}}


def from_peer(app: FastAPI, peer: str) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=(peer, 4000))
    return httpx.AsyncClient(transport=transport, base_url="http://keeper.test")


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def lookups(monkeypatch: pytest.MonkeyPatch, store: Any) -> list[str]:
    """Prefixes looked up in the store."""
    seen: list[str] = []
    async with store.unit() as unit:
        keys_class = type(unit.api_keys)
    original = keys_class.get_by_prefix

    async def counted(self: Any, prefix: str) -> Any:
        seen.append(prefix)
        return await original(self, prefix)

    monkeypatch.setattr(keys_class, "get_by_prefix", counted)
    return seen


@pytest.fixture
def app(make_app: AppFactory, store: Any, clock: Any, provider: Any) -> FastAPI:
    return make_app(auth_settings(), auth_store=store, clock=clock, http_client=provider.http)


@pytest.fixture
def valid(store: Any, codec: ApiKeyCodec) -> str:
    store.add_principal("svc_agent", PrincipalType.SERVICE)
    token, _ = store.add_key(codec, "svc_agent")
    return token


async def test_an_address_is_refused_after_its_failed_attempts_before_any_lookup(
    app: FastAPI, codec: ApiKeyCodec, valid: str, lookups: list[str], store: Any
) -> None:
    async with from_peer(app, "198.51.100.7") as http:
        failed = [
            (await http.get(WHOAMI, headers=bearer(codec.generate().token))).status_code
            for _ in range(BURST)
        ]
        looked_up = len(lookups)
        audited = len(store.audit_rows)
        refused = await http.get(WHOAMI, headers=bearer(codec.generate().token))
        refused_valid = await http.get(WHOAMI, headers=bearer(valid))
    assert failed == [401] * BURST
    assert refused.status_code == refused_valid.status_code == 429
    assert int(refused.headers["retry-after"]) >= 1
    body = refused.json()
    assert body["code"] == "rate-limited"
    assert body["class"] == "auth_failures"
    assert body["retry_after_s"] == int(refused.headers["retry-after"])
    assert len(lookups) == looked_up  # throttled requests never reach the store
    assert len(store.audit_rows) == audited  # nor the audit log


async def test_another_address_with_a_valid_key_is_unaffected(
    app: FastAPI, codec: ApiKeyCodec, valid: str
) -> None:
    async with from_peer(app, "198.51.100.7") as attacker:
        for _ in range(BURST + 1):
            await attacker.get(WHOAMI, headers=bearer(codec.generate().token))
    async with from_peer(app, "203.0.113.20") as http:
        assert (await http.get(WHOAMI, headers=bearer(valid))).status_code == 200


async def test_a_key_prefix_is_refused_after_failures_from_many_addresses(
    app: FastAPI, codec: ApiKeyCodec, valid: str, lookups: list[str]
) -> None:
    guessed = valid[:-4] + "aaaa"  # the right prefix with a wrong secret
    statuses = []
    for n in range(BURST + 1):
        async with from_peer(app, f"192.0.2.{n + 1}") as http:
            statuses.append((await http.get(WHOAMI, headers=bearer(guessed))).status_code)
    assert statuses == [401] * BURST + [429]
    assert len(lookups) == BURST
    async with from_peer(app, "192.0.2.200") as http:
        other_key = await http.get(WHOAMI, headers=bearer(codec.generate().token))
    assert other_key.status_code == 401  # a fresh address and prefix are still checked


async def test_successful_authentication_takes_nothing(
    app: FastAPI, codec: ApiKeyCodec, valid: str
) -> None:
    async with from_peer(app, "198.51.100.7") as http:
        for _ in range(BURST * 3):
            assert (await http.get(WHOAMI, headers=bearer(valid))).status_code == 200
        failed = [
            (await http.get(WHOAMI, headers=bearer(codec.generate().token))).status_code
            for _ in range(BURST + 1)
        ]
    assert failed == [401] * BURST + [429]


async def test_requests_without_credentials_are_not_counted(app: FastAPI, valid: str) -> None:
    async with from_peer(app, "198.51.100.7") as http:
        for _ in range(BURST * 2):
            assert (await http.get(WHOAMI)).status_code == 401
        assert (await http.get(WHOAMI, headers=bearer(valid))).status_code == 200


async def test_ipv6_clients_share_the_bucket_of_their_network(
    app: FastAPI, codec: ApiKeyCodec
) -> None:
    statuses = []
    for n in range(BURST + 1):
        async with from_peer(app, f"2001:db8:1:2::{n + 1:x}") as http:
            statuses.append(
                (await http.get(WHOAMI, headers=bearer(codec.generate().token))).status_code
            )
    assert statuses == [401] * BURST + [429]
    async with from_peer(app, "2001:db8:1:3::1") as http:
        other = await http.get(WHOAMI, headers=bearer(codec.generate().token))
    assert other.status_code == 401


async def test_throttled_attempts_are_logged_at_debug_only(
    app: FastAPI, codec: ApiKeyCodec, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    async with from_peer(app, "198.51.100.7") as http:
        for _ in range(BURST):
            await http.get(WHOAMI, headers=bearer(codec.generate().token))
        caplog.clear()
        for _ in range(5):
            assert (
                await http.get(WHOAMI, headers=bearer(codec.generate().token))
            ).status_code == 429
    keeper_records = [r for r in caplog.records if r.name.startswith("lorebase.keeper.")]
    throttled = [r for r in keeper_records if r.getMessage() == "authentication throttled"]
    assert len(throttled) == 5
    assert all(r.levelno == logging.DEBUG for r in throttled)
    assert not [
        r
        for r in keeper_records
        if r.levelno >= logging.INFO
        and r.name in ("lorebase.keeper.auth", "lorebase.keeper.limits")
    ]


async def test_invalid_bearer_tokens_are_limited_by_address_in_oidc_mode(
    make_app: AppFactory, store: Any, clock: Any, provider: Any
) -> None:
    app = make_app(auth_settings("oidc"), auth_store=store, clock=clock, http_client=provider.http)
    forged = provider.token(aud="someone-else")
    async with from_peer(app, "198.51.100.7") as http:
        failed = [
            (await http.get(WHOAMI, headers=bearer(forged))).status_code for _ in range(BURST)
        ]
        fetches = len(provider.requests)
        refused = await http.get(WHOAMI, headers=bearer(forged))
        refused_good = await http.get(WHOAMI, headers=bearer(provider.token()))
    assert failed == [401] * BURST
    assert refused.status_code == refused_good.status_code == 429
    assert "retry-after" in refused.headers
    assert len(provider.requests) == fetches
    async with from_peer(app, "203.0.113.20") as http:
        assert (await http.get(WHOAMI, headers=bearer(provider.token()))).status_code == 200


async def test_both_credentials_are_limited_in_combined_mode(
    make_app: AppFactory, store: Any, clock: Any, provider: Any, codec: ApiKeyCodec
) -> None:
    app = make_app(
        auth_settings("api_key+oidc"), auth_store=store, clock=clock, http_client=provider.http
    )
    forged = provider.token(aud="someone-else")
    async with from_peer(app, "198.51.100.7") as http:
        statuses = [
            (await http.get(WHOAMI, headers=bearer(forged))).status_code,
            (await http.get(WHOAMI, headers=bearer(codec.generate().token))).status_code,
            (await http.get(WHOAMI, headers={"X-Api-Key": "lbk_garbage"})).status_code,
            (await http.get(WHOAMI, headers=bearer(provider.token()))).status_code,
        ]
    assert statuses == [401, 401, 401, 429]


async def test_limits_off_means_failures_are_not_limited(
    make_app: AppFactory, store: Any, clock: Any, codec: ApiKeyCodec
) -> None:
    settings = auth_settings()
    settings["limits"]["rate"]["enabled"] = False
    app = make_app(settings, auth_store=store, clock=clock)
    async with from_peer(app, "198.51.100.7") as http:
        statuses = {
            (await http.get(WHOAMI, headers=bearer(codec.generate().token))).status_code
            for _ in range(BURST * 2)
        }
    assert statuses == {401}
