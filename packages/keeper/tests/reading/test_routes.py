"""Read routes without a database: registration, OpenAPI and the 503 answer."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import httpx
from fastapi import FastAPI

READ_PATHS = {
    ("get", "/api/v1/domains/{domain_id}/kinds/{kind}/records"),
    ("get", "/api/v1/domains/{domain_id}/kinds/{kind}/records/{key}"),
    ("get", "/api/v1/domains/{domain_id}/kinds/{kind}/records/{key}/links/{relation}"),
    ("get", "/api/v1/domains/{domain_id}/relations/{relation}/links"),
    ("post", "/api/v1/domains/{domain_id}/query"),
    ("post", "/api/v1/domains/{domain_id}/traverse"),
    ("post", "/api/v1/domains/{domain_id}/neighborhood"),
    ("post", "/api/v1/domains/{domain_id}/search"),
    ("get", "/api/v1/domains/{domain_id}/branches/{branch}/log"),
    ("get", "/api/v1/domains/{domain_id}/revisions/{revision}"),
    ("get", "/api/v1/domains/{domain_id}/revisions/{revision}/changeset"),
    ("get", "/api/v1/domains/{domain_id}/revisions/{revision}/counts"),
    ("get", "/api/v1/domains/{domain_id}/diff"),
}


def test_read_routes_are_documented(app: FastAPI) -> None:
    paths = app.openapi()["paths"]
    documented = {(method, path) for path, item in paths.items() for method in item}
    assert documented >= READ_PATHS


async def test_reads_need_a_database(
    app: FastAPI, client_for: Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]
) -> None:
    async with client_for(app) as http:
        response = await http.get("/api/v1/domains/supply/kinds/Supplier/records")
        query = await http.post("/api/v1/domains/supply/query", json={"kind": "Supplier"})
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert query.status_code == 503
