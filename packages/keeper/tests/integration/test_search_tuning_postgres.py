"""Search with ranking knobs in the request (`tuning`) against the `supply` example."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from .read_fixtures import Seeded, keeper, seeded_database
from .test_agents_api import seed

pytestmark = pytest.mark.integration

SEARCH = "/api/v1/domains/supply/search"


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


async def test_tuning_changes_corrections_and_bounds_the_work(seeded: Seeded) -> None:
    async with keeper(seeded.app_url) as http:
        body = {"q": "Kavsen Aloys", "kinds": ["Supplier"]}
        default = (await http.post(SEARCH, json=body)).json()
        assert [c["word"] for c in default["corrections"]] == ["aloys"]
        strict = await http.post(SEARCH, json={**body, "tuning": {"trigram_threshold": 1.0}})
        assert strict.status_code == 200
        assert strict.json()["corrections"] == []
        off = await http.post(SEARCH, json={**body, "tuning": {"typo_candidates": 0}})
        assert off.json()["corrections"] == []
        weighted = await http.post(
            SEARCH, json={**body, "tuning": {"lexical_weight": 2.0, "rrf_k": 10, "candidates": 5}}
        )
        assert weighted.status_code == 200
        too_many = await http.post(SEARCH, json={**body, "tuning": {"candidates": 100_000}})
        assert too_many.status_code == 422
        assert too_many.json()["errors"][0]["pointer"] == "/body/tuning/candidates"
        loose = await http.post(SEARCH, json={**body, "tuning": {"trigram_threshold": 0.1}})
        assert loose.status_code == 422
        assert loose.json()["errors"][0]["pointer"] == "/body/tuning/trigram_threshold"
        unknown = await http.post(SEARCH, json={**body, "tuning": {"boost": 1}})
        assert unknown.status_code == 422
