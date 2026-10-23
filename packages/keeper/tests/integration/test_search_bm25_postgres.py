"""`search.backend: bm25` through Keeper (`lorebase_store.bm25`) against the `supply` example.

Without a BM25 index lexical search keeps the default ranking and `/readyz` says so. Where the
server offers and preloads `pg_search` (a ParadeDB image), `storage apply` builds the index and
lexical search finds what the default finds, masks included; skipped elsewhere.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from lorebase_store import bm25, storage
from lorebase_store.db import create_engine
from lorebase_store.settings import DatabaseSettings, StorageSettings, StoreSettings

from .read_fixtures import Seeded, keeper, seeded_database
from .test_agents_api import seed
from .test_sensitivity_api import NICKNAME, STALE, VAULT, _hidden, _post, _section
from .test_sensitivity_api import seed as sensitive_seed

pytestmark = pytest.mark.integration

SEARCH = "/api/v1/domains/supply/search"
BM25 = {"backend": "bm25"}
QUERIES: tuple[dict[str, Any], ...] = (
    {"q": "bearing", "kinds": ["Part"]},
    {"q": "roller bearing"},
    {"q": "Kavsen Aloys", "kinds": ["Supplier"]},
    {"q": "tapered roller bearing for the kiln", "kinds": ["Part"]},
    {"q": "bearing -roller", "kinds": ["Part"]},
    {"q": '"roller bearing"'},
)


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


@pytest.fixture(scope="module")
def sensitive(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, sensitive_seed)


@asynccontextmanager
async def _bm25_index(owner_url: str) -> AsyncIterator[None]:
    """The BM25 index on the database for the block, dropped afterwards; skips the test
    where the server cannot build it."""
    engine = create_engine(DatabaseSettings(), url=owner_url, pooled=False)
    try:
        async with engine.connect() as connection:
            available = await connection.scalar(
                text("SELECT 1 FROM pg_available_extensions WHERE name = 'pg_search'")
            )
            preload = str(await connection.scalar(text("SHOW shared_preload_libraries")))
        if not available or "pg_search" not in preload:
            pytest.skip("the server does not offer and preload pg_search (a ParadeDB image)")
        wanted = StoreSettings(storage=StorageSettings(bm25_index=True))
        report = await storage.apply(engine, wanted, switch_wait_s=0)
        assert [s.status for s in report.steps if s.step == "bm25 index"] == ["done"]
        bm25.forget()
        try:
            yield
        finally:
            await storage.apply(engine, StoreSettings(), switch_wait_s=0)
            bm25.forget()
    finally:
        await engine.dispose()


def _found(response: httpx.Response) -> list[tuple[str, str, str]]:
    assert response.status_code == 200, response.text
    return sorted((i["kind"], i["key"], i["match"]) for i in response.json()["items"])


async def test_without_an_index_bm25_falls_back_and_readiness_says_so(seeded: Seeded) -> None:
    bm25.forget()
    async with keeper(seeded.app_url) as default, keeper(seeded.app_url, search=BM25) as http:
        ready = await http.get("/readyz")
        assert ready.status_code == 200, ready.text
        check = ready.json()["checks"]["search_backend"]
        assert check["status"] == "degraded"
        assert "ranks with postgres" in check["detail"]
        assert "search_backend" not in (await default.get("/readyz")).json()["checks"]
        for body in QUERIES:
            expected = (await default.post(SEARCH, json=body)).json()
            found = (await http.post(SEARCH, json=body)).json()
            assert found["items"] == expected["items"], body


async def test_bm25_finds_what_the_default_finds(seeded: Seeded) -> None:
    async with (
        _bm25_index(seeded.owner_url),
        keeper(seeded.app_url) as default,
        keeper(seeded.app_url, search=BM25) as http,
    ):
        ready = await http.get("/readyz")
        assert ready.json()["checks"]["search_backend"] == {"status": "ok", "detail": "bm25"}
        for body in QUERIES:
            limited = {**body, "limit": 100}
            expected = await default.post(SEARCH, json=limited)
            found = await http.post(SEARCH, json=limited)
            assert _found(found) == _found(expected), body
            assert found.json()["corrections"] == expected.json()["corrections"], body
            assert found.json()["truncated_candidates"] is False, body
            assert all(i["snippet"] for i in found.json()["items"]), body


async def test_bm25_keeps_sensitive_values_out_of_search(sensitive: Seeded) -> None:
    async with _bm25_index(sensitive.owner_url):
        key = sensitive.facts["viewer"]
        async with keeper(sensitive.app_url, _section(), vault=VAULT, search=BM25) as http:
            found = await _post(http, key, "/search", {"q": NICKNAME})
            _hidden(found)
            assert found.json()["items"] == []
            stale = await _post(http, key, "/search", {"q": "0100", "branch": STALE})
            _hidden(stale)
            assert stale.json()["items"] == []
