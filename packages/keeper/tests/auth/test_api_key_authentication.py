"""API key authentication through the application."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_store.models.enums import PrincipalType

PEPPER = "test-pepper-0123456789-0123456789-abcdef"
API_KEYS: dict[str, Any] = {"mode": "api_key", "api_keys": {"pepper": PEPPER}}
PROBE = "/api/v1/auth/whoami"

ClientFor = Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]


@pytest.fixture
def agent(store: Any) -> str:
    """A service principal."""
    store.add_principal("svc_agent", PrincipalType.SERVICE, name="ingest agent")
    return "svc_agent"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_missing_credentials_get_401_with_a_challenge(
    auth_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    async with client_for(auth_app(API_KEYS)) as http:
        response = await http.get(PROBE)
    body = response.json()
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["www-authenticate"] == "Bearer"
    assert body["code"] == "unauthorized"
    assert body["request_id"]


async def test_bearer_and_x_api_key_both_authenticate(
    auth_app: Callable[..., FastAPI],
    client_for: ClientFor,
    store: Any,
    codec: ApiKeyCodec,
    agent: str,
) -> None:
    token, _ = store.add_key(codec, agent)
    async with client_for(auth_app(API_KEYS)) as http:
        assert (await http.get(PROBE, headers=bearer(token))).status_code == 200
        assert (await http.get(PROBE, headers={"X-Api-Key": token})).status_code == 200


async def test_whoami_describes_the_key_owner(
    auth_app: Callable[..., FastAPI],
    client_for: ClientFor,
    store: Any,
    codec: ApiKeyCodec,
    agent: str,
) -> None:
    token, key = store.add_key(codec, agent, scopes=["domain:acme"])
    store.group_rows["grp_bots"] = None
    store.members.add(("grp_bots", agent))
    async with client_for(auth_app(API_KEYS)) as http:
        body = (await http.get(PROBE, headers=bearer(token))).json()
    assert body == {
        "id": "svc_agent",
        "type": "service",
        "display_name": "ingest agent",
        "method": "api_key",
        "subjects": ["svc_agent", "grp_bots"],
        "key_scopes": ["domain:acme"],
        "installation_admin": False,
    }
    assert key.id not in str(body)


async def test_wrong_secret_and_unknown_prefix_look_the_same(
    auth_app: Callable[..., FastAPI],
    client_for: ClientFor,
    store: Any,
    codec: ApiKeyCodec,
    agent: str,
) -> None:
    token, _ = store.add_key(codec, agent)
    wrong_secret = token[:-4] + "aaaa"
    unknown = codec.generate().token
    async with client_for(auth_app(API_KEYS)) as http:
        first = await http.get(PROBE, headers=bearer(wrong_secret))
        second = await http.get(PROBE, headers=bearer(unknown))
        third = await http.get(PROBE, headers=bearer("lbk_garbage"))
    assert first.status_code == second.status_code == third.status_code == 401
    assert first.json()["detail"] == second.json()["detail"] == third.json()["detail"]
    assert 'error="invalid_token"' in first.headers["www-authenticate"]


async def test_revoked_key_is_rejected(
    auth_app: Callable[..., FastAPI],
    client_for: ClientFor,
    store: Any,
    codec: ApiKeyCodec,
    agent: str,
    clock: Any,
) -> None:
    token, _ = store.add_key(codec, agent, revoked_at=clock.now)
    async with client_for(auth_app(API_KEYS)) as http:
        assert (await http.get(PROBE, headers=bearer(token))).status_code == 401


async def test_key_stops_working_when_it_expires(
    auth_app: Callable[..., FastAPI],
    client_for: ClientFor,
    store: Any,
    codec: ApiKeyCodec,
    agent: str,
    clock: Any,
) -> None:
    token, _ = store.add_key(codec, agent, expires_at=clock.now + timedelta(days=1))
    async with client_for(auth_app(API_KEYS)) as http:
        assert (await http.get(PROBE, headers=bearer(token))).status_code == 200
        clock.advance(days=1, seconds=1)
        assert (await http.get(PROBE, headers=bearer(token))).status_code == 401


async def test_last_used_is_updated_at_most_once_per_interval(
    auth_app: Callable[..., FastAPI],
    client_for: ClientFor,
    store: Any,
    codec: ApiKeyCodec,
    agent: str,
    clock: Any,
) -> None:
    token, key = store.add_key(codec, agent)
    async with client_for(auth_app(API_KEYS)) as http:
        for _ in range(3):
            await http.get(PROBE, headers=bearer(token))
        assert store.touches == 1
        assert store.key_rows[key.id].last_used_at == clock.now
        clock.advance(seconds=61)
        await http.get(PROBE, headers=bearer(token))
    assert store.touches == 2


async def test_failure_to_record_use_does_not_fail_the_request(
    auth_app: Callable[..., FastAPI],
    client_for: ClientFor,
    store: Any,
    codec: ApiKeyCodec,
    agent: str,
) -> None:
    token, _ = store.add_key(codec, agent)
    store.touch_failure = RuntimeError("database down")
    async with client_for(auth_app(API_KEYS)) as http:
        assert (await http.get(PROBE, headers=bearer(token))).status_code == 200


async def test_unsupported_authorization_scheme_is_rejected(
    auth_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    async with client_for(auth_app(API_KEYS)) as http:
        response = await http.get(PROBE, headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code == 401


async def test_api_key_mode_does_not_accept_jwts(
    auth_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    async with client_for(auth_app(API_KEYS)) as http:
        response = await http.get(PROBE, headers=bearer("aaa.bbb.ccc"))
    assert response.status_code == 401


async def test_mode_none_needs_no_credentials(
    auth_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    async with client_for(auth_app({"mode": "none"})) as http:
        response = await http.get(PROBE)
    assert response.status_code == 200
    assert response.json()["id"] == "anonymous"
    assert response.json()["method"] == "none"


def test_authenticated_modes_need_a_database(make_app: Callable[..., FastAPI]) -> None:
    from lorebase_platform.settings import ConfigError

    with pytest.raises(ConfigError, match=r"keeper\.auth\.mode.*database"):
        make_app({"auth": API_KEYS})
