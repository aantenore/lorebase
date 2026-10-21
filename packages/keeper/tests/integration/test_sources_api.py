"""Source synchronization trigger and run listing against the `supply` example in Postgres."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.models.enums import RunStatus
from lorebase_store.repositories import SourceRunRepository, SourceRunRow, scope_to_domain
from lorebase_store.runtime import StoreRuntime

from .read_fixtures import Seeded, keeper, owner_session, seeded_database
from .supply import DOMAIN, Clock, load_supply

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}/sources"
SUPPLIERS = "approved.suppliers"


async def seed(
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema, _ = await load_supply(session, runtime, clock)
    return Seeded(owner_url=owner_url, app_url=app_url, schema=schema)


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


async def test_sync_queues_one_job_per_request_kind(seeded: Seeded) -> None:
    source = seeded.schema.source_by_api_name(SUPPLIERS)
    assert source is not None
    async with keeper(seeded.app_url) as http:
        first = await http.post(f"{BASE}/{SUPPLIERS}/sync")
        assert first.status_code == 202, first.text
        body = first.json()
        assert body["source"] == source.id
        assert body["api_name"] == SUPPLIERS
        assert body["dry_run"] is False
        again = await http.post(f"{BASE}/{source.id}/sync", json={"dry_run": False})
        assert again.json()["job_id"] == body["job_id"]  # the pending job is reused
        dry = await http.post(f"{BASE}/{SUPPLIERS}/sync", json={"dry_run": True})
        assert dry.status_code == 202
        assert dry.json()["job_id"] != body["job_id"]
        missing = await http.post(f"{BASE}/nowhere.at_all/sync")
        assert missing.status_code == 404
        invalid = await http.post(f"{BASE}/{SUPPLIERS}/sync", json={"dryrun": True})
        assert invalid.status_code == 422
    async with owner_session(seeded.owner_url) as session, session.begin():
        jobs = (
            await session.execute(
                text("SELECT type, domain_id, payload FROM jobs WHERE id = :i"),
                {"i": body["job_id"]},
            )
        ).one()
        actions = (
            await session.execute(
                text(
                    "SELECT target, details FROM audit_log WHERE domain_id = :d "
                    "AND action = 'source.sync' ORDER BY seq"
                ),
                {"d": DOMAIN},
            )
        ).all()
    assert tuple(jobs) == ("source_sync", DOMAIN, {"source": source.id, "dry_run": False})
    assert [target for target, _ in actions] == [source.id] * 3


async def test_runs_are_listed_newest_first(seeded: Seeded) -> None:
    source = seeded.schema.source_by_api_name(SUPPLIERS)
    assert source is not None
    async with owner_session(seeded.owner_url) as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        runs = SourceRunRepository(session, DOMAIN)
        for index in range(3):
            await runs.insert(
                SourceRunRow(
                    id=f"srn_{index}",
                    source_id=source.id,
                    status=RunStatus.SUCCEEDED,
                    started_at=datetime(2026, 10, 1, 8, index, tzinfo=UTC),
                    finished_at=datetime(2026, 10, 1, 8, index, 30, tzinfo=UTC),
                    stats={"counts": {"added": index}},
                    revision_id=bytes(32),
                )
            )
    async with keeper(seeded.app_url) as http:
        page = (await http.get(f"{BASE}/{SUPPLIERS}/runs", params={"limit": 2})).json()
        assert [item["id"] for item in page["items"]] == ["srn_2", "srn_1"]
        assert page["items"][0]["stats"] == {"counts": {"added": 2}}
        assert page["items"][0]["revision"] == "00" * 32
        rest = (
            await http.get(
                f"{BASE}/{SUPPLIERS}/runs", params={"limit": 2, "cursor": page["next_cursor"]}
            )
        ).json()
        assert [item["id"] for item in rest["items"]] == ["srn_0"]
        assert rest["next_cursor"] is None
        assert (await http.get(f"{BASE}/nowhere.at_all/runs")).status_code == 404
