from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Response
from pydantic import ValidationError

from lorebase_keeper.settings import KeeperSettings

AppFactory = Callable[..., FastAPI]
ClientFactory = Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]

HSTS = "strict-transport-security"
HSTS_DEFAULT = "max-age=31536000; includeSubDomains"


def hsts(**values: Any) -> dict[str, Any]:
    return {"security_headers": {"hsts": values}}


async def fetch(
    make_app: AppFactory,
    client_for: ClientFactory,
    keeper: dict[str, Any] | None = None,
    *,
    base_url: str = "http://testserver",
    headers: dict[str, str] | None = None,
    path: str = "/healthz",
) -> httpx.Response:
    async with client_for(make_app(keeper)) as client:
        return await client.get(f"{base_url}{path}", headers=headers)


async def test_default_headers(make_app: AppFactory, client_for: ClientFactory) -> None:
    response = await fetch(make_app, client_for)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_headers_on_error_responses(make_app: AppFactory, client_for: ClientFactory) -> None:
    response = await fetch(make_app, client_for, path="/missing")
    assert response.status_code == 404
    assert response.headers["x-frame-options"] == "DENY"


async def test_hsts_auto_is_absent_over_http(
    make_app: AppFactory, client_for: ClientFactory
) -> None:
    response = await fetch(make_app, client_for)
    assert HSTS not in response.headers


async def test_hsts_auto_is_sent_over_https(
    make_app: AppFactory, client_for: ClientFactory
) -> None:
    response = await fetch(make_app, client_for, base_url="https://testserver")
    assert response.headers[HSTS] == HSTS_DEFAULT


async def test_hsts_auto_follows_forwarded_proto_of_a_trusted_proxy_only(
    make_app: AppFactory, client_for: ClientFactory
) -> None:
    forwarded = {"X-Forwarded-Proto": "https"}
    untrusted = await fetch(make_app, client_for, headers=forwarded)
    assert HSTS not in untrusted.headers
    # The test transport connects from 127.0.0.1.
    trusted_proxy = {"trusted_proxies": ["127.0.0.1"]}
    trusted = await fetch(make_app, client_for, trusted_proxy, headers=forwarded)
    assert trusted.headers[HSTS] == HSTS_DEFAULT
    plain = await fetch(make_app, client_for, trusted_proxy, headers={"X-Forwarded-Proto": "http"})
    assert HSTS not in plain.headers


async def test_hsts_always_and_never(make_app: AppFactory, client_for: ClientFactory) -> None:
    always = await fetch(make_app, client_for, hsts(enabled="always"))
    assert HSTS in always.headers
    never = await fetch(
        make_app,
        client_for,
        hsts(enabled="never"),
        base_url="https://testserver",
    )
    assert HSTS not in never.headers


async def test_hsts_directives(make_app: AppFactory, client_for: ClientFactory) -> None:
    custom = hsts(enabled="always", max_age_s=63072000, include_subdomains=False)
    response = await fetch(make_app, client_for, custom)
    assert response.headers[HSTS] == "max-age=63072000"
    preload = await fetch(make_app, client_for, hsts(enabled="always", preload=True))
    assert preload.headers[HSTS] == f"{HSTS_DEFAULT}; preload"


async def test_frame_options_and_referrer_policy_are_configurable(
    make_app: AppFactory, client_for: ClientFactory
) -> None:
    same = await fetch(
        make_app,
        client_for,
        {"security_headers": {"frame_options": "SAMEORIGIN", "referrer_policy": "same-origin"}},
    )
    assert same.headers["x-frame-options"] == "SAMEORIGIN"
    assert same.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert same.headers["referrer-policy"] == "same-origin"
    off = await fetch(
        make_app,
        client_for,
        {"security_headers": {"frame_options": "off", "referrer_policy": ""}},
    )
    for name in ("x-frame-options", "content-security-policy", "referrer-policy"):
        assert name not in off.headers


async def test_disabled(make_app: AppFactory, client_for: ClientFactory) -> None:
    response = await fetch(
        make_app,
        client_for,
        {"security_headers": {"enabled": False, "hsts": {"enabled": "always"}}},
    )
    for name in ("x-content-type-options", "x-frame-options", "content-security-policy", HSTS):
        assert name not in response.headers


async def test_a_header_set_by_a_route_is_kept(
    make_app: AppFactory, client_for: ClientFactory
) -> None:
    app = make_app()

    @app.get("/custom")
    async def custom(response: Response) -> dict[str, str]:
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "origin"
        return {}

    async with client_for(app) as client:
        response = await client.get("/custom")
    assert response.headers.get_list("x-frame-options") == ["SAMEORIGIN"]
    assert response.headers.get_list("referrer-policy") == ["origin"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_preload_needs_a_long_max_age_and_subdomains() -> None:
    with pytest.raises(ValidationError):
        KeeperSettings.model_validate(hsts(preload=True, max_age_s=60))
    with pytest.raises(ValidationError):
        KeeperSettings.model_validate(hsts(preload=True, include_subdomains=False))
