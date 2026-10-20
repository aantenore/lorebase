"""Response cache and conditional GET of reads."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

from lorebase_keeper.reading.caching import (
    CachedResponse,
    CacheKey,
    InMemoryResponseCache,
    ResponseCache,
    respond,
)
from lorebase_keeper.settings import ReadCacheSettings


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def key(revision: str = "r1", path: str = "/a", **query: str) -> CacheKey:
    return CacheKey("supply", revision, path, tuple(sorted(query.items())))


def entry(size: int, *, time_travel: bool = False) -> CachedResponse:
    return CachedResponse(b"x" * size, {}, time_travel)


async def test_hit_miss_and_revision_in_the_key() -> None:
    cache = InMemoryResponseCache(ReadCacheSettings())
    assert await cache.get(key()) is None
    await cache.put(key(), entry(3))
    assert await cache.get(key()) == entry(3)
    assert await cache.get(key(revision="r2")) is None
    assert await cache.get(key(limit="5")) is None


async def test_eviction_by_entries_bytes_and_time() -> None:
    clock = Clock()
    settings = ReadCacheSettings(max_entries=2, max_bytes=10, ttl_s=5, max_body_bytes=8)
    cache = InMemoryResponseCache(settings, clock=clock)
    await cache.put(key(path="/1"), entry(4))
    await cache.put(key(path="/2"), entry(4))
    assert await cache.get(key(path="/1")) is not None  # now most recently used
    await cache.put(key(path="/3"), entry(4))
    assert await cache.get(key(path="/2")) is None  # least recently used went first
    assert len(cache) == 2
    await cache.put(key(path="/4"), entry(8))  # bytes bound: 4 + 8 > 10
    assert len(cache) == 1
    assert cache.size_bytes == 8
    await cache.put(key(path="/5"), entry(9))  # above max_body_bytes: not kept
    assert await cache.get(key(path="/5")) is None
    clock.now = 6
    assert await cache.get(key(path="/4")) is None
    assert cache.size_bytes == 0


async def test_time_travel_entries_are_capped() -> None:
    cache = InMemoryResponseCache(ReadCacheSettings(time_travel_max_entries=1))
    await cache.put(key(path="/t1"), entry(1, time_travel=True))
    await cache.put(key(path="/t2"), entry(1, time_travel=True))
    await cache.put(key(path="/h"), entry(1))
    assert await cache.get(key(path="/t1")) is not None
    assert await cache.get(key(path="/t2")) is None
    assert await cache.get(key(path="/h")) is not None


class Item(BaseModel):
    revision: str
    calls: int


@pytest.fixture
async def served() -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, int]]]:
    """A route answering through `respond`, its Revision taken from the query."""
    cache: ResponseCache = InMemoryResponseCache(ReadCacheSettings())
    calls = {"n": 0}
    app = FastAPI()

    @app.get("/items/{name}")
    async def item(name: str, request: Request, rev: str) -> Response:
        async def produce() -> Item:
            calls["n"] += 1
            return Item(revision=rev, calls=calls["n"])

        return await respond(
            request,
            cache,
            domain_id="supply",
            revision=rev,
            produce=produce,
            headers={"Lorebase-Revision": rev},
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http, calls


async def test_respond_serves_304_cached_bodies_and_misses_on_new_revisions(
    served: tuple[httpx.AsyncClient, dict[str, int]],
) -> None:
    http, calls = served
    first = await http.get("/items/a", params={"rev": "r1"})
    assert first.status_code == 200
    assert first.json() == {"revision": "r1", "calls": 1}
    tag = first.headers["ETag"]
    assert tag.startswith('"r1.')
    assert first.headers["Cache-Control"] == "private, must-revalidate"
    assert first.headers["Lorebase-Revision"] == "r1"

    hit = await http.get("/items/a", params={"rev": "r1"})
    assert hit.json() == first.json()
    assert hit.headers["ETag"] == tag
    assert calls["n"] == 1

    not_modified = await http.get("/items/a", params={"rev": "r1"}, headers={"If-None-Match": tag})
    assert not_modified.status_code == 304
    assert not_modified.headers["ETag"] == tag
    assert not_modified.content == b""
    assert calls["n"] == 1

    moved = await http.get("/items/a", params={"rev": "r2"}, headers={"If-None-Match": tag})
    assert moved.status_code == 200
    assert moved.json() == {"revision": "r2", "calls": 2}
    assert moved.headers["ETag"] != tag

    other = await http.get("/items/b", params={"rev": "r1"})
    assert other.json()["calls"] == 3
