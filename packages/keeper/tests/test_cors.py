"""Cross-origin access: configured headers add to the required ones, ETag is readable."""

from __future__ import annotations

from .conftest import AppFactory, ClientFactory

ORIGIN = "http://localhost:5173"


def _listed(value: str | None) -> set[str]:
    return {part.strip().lower() for part in (value or "").split(",") if part.strip()}


async def test_configured_allow_headers_add_to_the_required_ones(
    make_app: AppFactory, client_for: ClientFactory
) -> None:
    app = make_app({"cors": {"allow_origins": [ORIGIN], "allow_headers": ["X-Custom"]}})
    asked = (
        "authorization, content-type, idempotency-key, if-none-match, x-lorebase-client, x-custom"
    )
    async with client_for(app) as client:
        response = await client.options(
            "/healthz",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": asked,
            },
        )
    assert response.status_code == 200, response.text
    assert _listed(asked) <= _listed(response.headers.get("access-control-allow-headers"))


async def test_conditional_read_headers_are_exposed_by_default(
    make_app: AppFactory, client_for: ClientFactory
) -> None:
    app = make_app({"cors": {"allow_origins": [ORIGIN]}})
    async with client_for(app) as client:
        response = await client.get("/healthz", headers={"Origin": ORIGIN})
    assert response.headers["access-control-allow-origin"] == ORIGIN
    exposed = _listed(response.headers.get("access-control-expose-headers"))
    assert {"etag", "lorebase-revision", "x-request-id", "retry-after"} <= exposed


async def test_configured_expose_headers_add_to_the_required_ones(
    make_app: AppFactory, client_for: ClientFactory
) -> None:
    app = make_app({"cors": {"allow_origins": [ORIGIN], "expose_headers": ["Location"]}})
    async with client_for(app) as client:
        response = await client.get("/healthz", headers={"Origin": ORIGIN})
    exposed = _listed(response.headers.get("access-control-expose-headers"))
    assert {"location", "etag", "lorebase-revision"} <= exposed


async def test_no_cors_headers_without_allowed_origins(
    make_app: AppFactory, client_for: ClientFactory
) -> None:
    async with client_for(make_app()) as client:
        response = await client.get("/healthz", headers={"Origin": ORIGIN})
    assert "access-control-allow-origin" not in response.headers
