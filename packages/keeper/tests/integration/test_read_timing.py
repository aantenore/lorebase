"""Timings of list pages, search, Links, neighborhoods and conditional GETs over ~10k Parts,
printed for the record (not asserted).

Performance targets are asserted by the performance baseline (T28), not here.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.runtime import StoreRuntime

from .read_fixtures import Seeded, keeper, seeded_database
from .supply import DOMAIN, Clock, commit_changes, load_supply, synthetic_parts

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
EXTRA_PARTS = 10_000
ROUNDS = 5


async def seed(
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema, _ = await load_supply(session, runtime, clock)
    started = time.perf_counter()
    await commit_changes(
        session,
        runtime,
        clock,
        synthetic_parts(schema, EXTRA_PARTS),
        message="Synthetic parts",
        origin="import",
    )
    seeded = Seeded(owner_url=owner_url, app_url=app_url, schema=schema)
    seeded.facts["commit_s"] = time.perf_counter() - started
    return seeded


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


def _report(label: str, samples: list[float]) -> None:
    samples.sort()
    print(
        f"\n[timing] {label}: median {samples[len(samples) // 2] * 1000:.1f} ms, "
        f"max {samples[-1] * 1000:.1f} ms over {len(samples)} runs"
    )


async def _timed(call: Any) -> list[float]:
    await call()  # warm caches and pools
    samples = []
    for _ in range(ROUNDS):
        started = time.perf_counter()
        await call()
        samples.append(time.perf_counter() - started)
    return samples


async def test_print_list_and_search_timings(seeded: Seeded) -> None:
    print(f"\n[timing] commit of {EXTRA_PARTS} Parts: {seeded.facts['commit_s']:.2f} s")
    async with keeper(seeded.app_url) as http:
        params = {
            "where": '{"attr": "category", "op": "eq", "value": "bearing"}',
            "order_by": "unit_cost:desc",
            "limit": 50,
        }
        first = await http.get(f"{BASE}/kinds/Part/records", params=params)
        assert first.status_code == 200
        assert len(first.json()["items"]) == 50
        cursor = first.json()["next_cursor"]

        async def list_page() -> None:
            response = await http.get(f"{BASE}/kinds/Part/records", params=params)
            assert response.status_code == 200

        async def next_page() -> None:
            response = await http.get(
                f"{BASE}/kinds/Part/records", params={**params, "cursor": cursor}
            )
            assert response.status_code == 200

        async def plain_page() -> None:
            response = await http.get(f"{BASE}/kinds/Part/records", params={"limit": 50})
            assert response.status_code == 200

        async def search() -> None:
            response = await http.post(
                f"{BASE}/search", json={"q": "synthetic bearing", "kinds": ["Part"], "limit": 20}
            )
            assert response.status_code == 200
            assert response.json()["items"]

        async def get_one() -> None:
            response = await http.get(f"{BASE}/kinds/Part/records/P-55555")
            assert response.status_code == 200

        _report("list page, 50 of ~10k Parts by key", await _timed(plain_page))
        _report("filtered and ordered list page, 50 rows", await _timed(list_page))
        _report("second page by cursor", await _timed(next_page))
        _report("full-text search, 20 results", await _timed(search))
        _report("Record get by key", await _timed(get_one))


async def test_print_link_neighborhood_and_conditional_timings(seeded: Seeded) -> None:
    """Reads served by the store primitives (response cache off), and a conditional GET
    answered from the head Revision."""
    async with keeper(seeded.app_url, {"reads": {"cache": {"enabled": False}}}) as http:

        async def inbound_links() -> None:
            response = await http.get(
                f"{BASE}/kinds/Supplier/records/S-1001/links/purchase_orders",
                params={"limit": 50},
            )
            assert response.status_code == 200
            assert response.json()["items"]

        async def neighborhood() -> None:
            response = await http.post(
                f"{BASE}/neighborhood",
                json={"seeds": [{"kind": "Part", "key": "P-10001"}], "depth": 2},
            )
            assert response.status_code == 200
            assert response.json()["edges"]

        path = f"{BASE}/kinds/Part/records/P-10001"
        tag = (await http.get(path)).headers["ETag"]

        async def conditional_get() -> None:
            response = await http.get(path, headers={"If-None-Match": tag})
            assert response.status_code == 304

        _report("incoming foreign-key Links, page of 50", await _timed(inbound_links))
        _report("neighborhood, depth 2", await _timed(neighborhood))
        _report("conditional GET answered 304", await _timed(conditional_get))
