from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from lorebase_keeper.deps import IdempotencyKeyDep
from lorebase_keeper.pagination import (
    Page,
    PageParamsDep,
    decode_cursor,
    encode_cursor,
)

ClientFor = Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]


@pytest.fixture
async def listing(
    make_app: Callable[..., FastAPI], client_for: ClientFor
) -> AsyncIterator[httpx.AsyncClient]:
    app = make_app({"pagination": {"default_limit": 3, "max_limit": 5}})

    async def numbers(params: PageParamsDep) -> Page[int]:
        start = (params.position or {"after": 0})["after"]
        items = list(range(start + 1, start + 1 + params.limit))
        return Page[int](items=items, next_cursor=encode_cursor({"after": items[-1]}))

    async def write(key: IdempotencyKeyDep) -> dict[str, str | None]:
        return {"key": key}

    app.add_api_route("/numbers", numbers)
    app.add_api_route("/write", write, methods=["POST"])
    async with client_for(app) as http:
        yield http


def test_cursor_round_trip() -> None:
    position: dict[str, Any] = {"after": "abc", "n": 7}
    assert decode_cursor(encode_cursor(position)) == position


@pytest.mark.parametrize("cursor", ["!!!", "bm90LWpzb24", "WzEsMl0"])
def test_foreign_cursor_is_rejected(cursor: str) -> None:
    with pytest.raises(ValueError, match="cursor"):
        decode_cursor(cursor)


async def test_default_limit_and_cursor_follow_up(listing: httpx.AsyncClient) -> None:
    first = (await listing.get("/numbers")).json()
    assert first["items"] == [1, 2, 3]
    second = (await listing.get("/numbers", params={"cursor": first["next_cursor"]})).json()
    assert second["items"] == [4, 5, 6]


async def test_limit_above_maximum_is_a_validation_problem(listing: httpx.AsyncClient) -> None:
    response = await listing.get("/numbers", params={"limit": 6})
    body = response.json()
    assert response.status_code == 422
    assert body["errors"][0]["pointer"] == "/query/limit"


async def test_invalid_cursor_is_a_validation_problem(listing: httpx.AsyncClient) -> None:
    response = await listing.get("/numbers", params={"cursor": "garbage!"})
    assert response.status_code == 422
    assert response.json()["errors"][0]["pointer"] == "/query/cursor"


async def test_idempotency_key_is_optional_and_validated(listing: httpx.AsyncClient) -> None:
    assert (await listing.post("/write")).json() == {"key": None}
    ok = await listing.post("/write", headers={"Idempotency-Key": "order-42:retry.1"})
    assert ok.json() == {"key": "order-42:retry.1"}
    bad = await listing.post("/write", headers={"Idempotency-Key": "has space"})
    assert bad.status_code == 422
    assert bad.headers["content-type"] == "application/problem+json"
