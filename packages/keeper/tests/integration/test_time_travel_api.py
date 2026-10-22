"""Reads at a past Revision that need materialized rows, served from a snapshot of it.

Each read at the `data` Revision of `main` (whose head has moved on since) must answer what a
head read answered when `data` was the head: the branch `pinned`, created at `data`, keeps
that head, so its answers are the reference.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.materialize import create_branch
from lorebase_store.runtime import StoreRuntime

from .read_fixtures import Seeded, keeper, owner_session, seeded_database
from .supply import DOMAIN, Clock, commit_changes, load_supply, record_change, rows

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
PINNED = "pinned"
SERVED = ("revision", "served_from", "branch", "etag")
TIER_ONE = {"kind": "Supplier", "where": {"attr": "tier", "op": "eq", "value": "1"}}


async def seed(
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema, (_, schema_commit, data) = await load_supply(session, runtime, clock)
    supplier = schema.kind_by_api_name("Supplier")
    assert supplier is not None
    original = next(r for r in rows("supplier.csv") if r["supplier_code"] == "S-1001")
    updated = await commit_changes(
        session,
        runtime,
        clock,
        [record_change(supplier, {**original, "risk_score": "90", "legal_name": "Kavsen Steel"})],
        message="Raise the risk of S-1001 and rename it",
    )
    other = next(r for r in rows("supplier.csv") if r["supplier_code"] == "S-1002")
    await commit_changes(
        session,
        runtime,
        clock,
        [record_change(supplier, {**other, "risk_score": "11"})],
        message="Lower the risk of S-1002",
    )
    await create_branch(
        session,
        DOMAIN,
        PINNED,
        created_by="usr_editor",
        start=data.revision_id,
        runtime=runtime,
        clock=clock,
    )
    return Seeded(
        owner_url=owner_url,
        app_url=app_url,
        schema=schema,
        revisions={
            "schema": schema_commit.revision_id.hex(),
            "data": data.revision_id.hex(),
            "updated": updated.revision_id.hex(),
        },
    )


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


@pytest.fixture
async def http(seeded: Seeded) -> Any:
    async with keeper(seeded.app_url) as client:
        yield client


def _content(body: Any) -> Any:
    """A response body without what names where it was served from."""
    return {k: v for k, v in body.items() if k not in SERVED}


async def _get(http: httpx.AsyncClient, path: str, **params: Any) -> Any:
    response = await http.get(BASE + path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def _post(http: httpx.AsyncClient, path: str, body: dict[str, Any]) -> Any:
    response = await http.post(BASE + path, json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def _past_and_pinned_get(
    http: httpx.AsyncClient, seeded: Seeded, path: str, **params: Any
) -> tuple[Any, Any]:
    past = await _get(http, path, at=seeded.revisions["data"], **params)
    pinned = await _get(http, path, branch=PINNED, **params)
    assert past["served_from"] == "tree"
    assert past["revision"] == seeded.revisions["data"]
    assert pinned["served_from"] == "materialized"
    return past, pinned


async def _past_and_pinned_post(
    http: httpx.AsyncClient, seeded: Seeded, path: str, body: dict[str, Any]
) -> tuple[Any, Any]:
    past = await _post(http, path, {**body, "at": seeded.revisions["data"]})
    pinned = await _post(http, path, {**body, "branch": PINNED})
    return past, pinned


async def _snapshots(seeded: Seeded) -> list[str]:
    async with owner_session(seeded.owner_url) as session, session.begin():
        found = await session.execute(
            text("SELECT name FROM refs WHERE domain_id = :d AND kind = 'snapshot' ORDER BY name"),
            {"d": DOMAIN},
        )
        return [name for (name,) in found]


async def test_filters_ordering_includes_and_aggregates(
    http: httpx.AsyncClient, seeded: Seeded
) -> None:
    risky = '{"attr":"risk_score","op":"gte","value":"50"}'
    past, pinned = await _past_and_pinned_get(
        http, seeded, "/kinds/Supplier/records", where=risky, order_by="risk_score:desc", limit=3
    )
    assert _content(past) == _content(pinned)
    assert "S-1001" not in [item["key"] for item in past["items"]]
    head = await _get(http, "/kinds/Supplier/records", where=risky, limit=100)
    assert "S-1001" in [item["key"] for item in head["items"]]  # risk 90 since `updated`

    cursor = past["next_cursor"]
    assert cursor is not None
    following, reference = await _past_and_pinned_get(
        http,
        seeded,
        "/kinds/Supplier/records",
        where=risky,
        order_by="risk_score:desc",
        limit=3,
        cursor=cursor,
    )
    assert _content(following) == _content(reference)

    body = {
        "kind": "Part",
        "where": {
            "and": [
                {"via": "supplied_by", "where": {"attr": "country", "op": "in", "value": ["PL"]}},
                {"count": "supplied_by", "op": "gte", "value": 1},
            ]
        },
        "select": ["name"],
        "include": {"supplied_by": {"select": ["country", "legal_name"], "limit": 2}},
        "order_by": [{"attr": "name", "dir": "desc"}],
        "limit": 100,
        "aggregate": {"group_by": "category"},
    }
    past, pinned = await _past_and_pinned_post(http, seeded, "/query", body)
    assert past["served_from"] == "tree"
    assert past["items"]
    assert past["aggregate"]["counts"]
    assert _content(past) == _content(pinned)
    names = {
        i["key"]: i["values"].get("legal_name")
        for item in past["items"]
        for i in item["included"]["supplied_by"]
    }
    assert names.get("S-1001", "Kavsen Alloys Sp. z o.o.") == "Kavsen Alloys Sp. z o.o."

    trait, pinned_trait = await _past_and_pinned_post(
        http,
        seeded,
        "/query",
        {"trait": "Located", "where": {"attr": "country", "op": "eq", "value": "PL"}},
    )
    assert trait["items"]
    assert _content(trait) == _content(pinned_trait)


async def test_link_and_lineage_summaries(http: httpx.AsyncClient, seeded: Seeded) -> None:
    past, pinned = await _past_and_pinned_get(
        http, seeded, "/kinds/Supplier/records/S-1001", links="true", lineage="true"
    )
    assert _content(past) == _content(pinned)
    assert past["record"]["values"]["risk_score"] == "32"
    assert past["record"]["links"]
    assert past["record"]["lineage"]


async def test_traversal_neighborhood_and_search(http: httpx.AsyncClient, seeded: Seeded) -> None:
    traversal = {
        "start": {"kind": "Part", "key": "P-10001"},
        "path": [
            {"relation": "supplied_by"},
            {"relation": "contracts", "where": {"attr": "status", "op": "eq", "value": "active"}},
        ],
    }
    past, pinned = await _past_and_pinned_post(http, seeded, "/traverse", traversal)
    assert past["hops"][0]["records"]
    assert _content(past) == _content(pinned)

    hood = {"seeds": [{"kind": "Supplier", "key": "S-1001"}], "depth": 2}
    past, pinned = await _past_and_pinned_post(http, seeded, "/neighborhood", hood)
    assert _content(past) == _content(pinned)
    titles = {n["key"]: n.get("title") for n in past["nodes"]}
    assert titles["S-1001"] == "Kavsen Alloys Sp. z o.o."

    for body in (
        {"q": "Kavsen", "kinds": ["Supplier"]},
        {"q": "Kavsen Aloys", "kinds": ["Supplier"]},
        {"q": "bearing", "kinds": ["Part"], "limit": 5},
        {
            "q": "bearing",
            "kinds": ["Part"],
            "where": {"attr": "lifecycle", "op": "eq", "value": "active"},
        },
    ):
        past, pinned = await _past_and_pinned_post(http, seeded, "/search", body)
        assert past["items"], body
        assert _content(past) == _content(pinned)
    head = await _post(http, "/search", {"q": "Kavsen Steel", "kinds": ["Supplier"]})
    assert [hit["key"] for hit in head["items"]][:1] == ["S-1001"]
    past = await _post(
        http,
        "/search",
        {"q": "Steel", "kinds": ["Supplier"], "at": seeded.revisions["data"]},
    )
    assert "S-1001" not in [hit["key"] for hit in past["items"]]


async def test_record_history_up_to_a_revision(http: httpx.AsyncClient, seeded: Seeded) -> None:
    path = "/kinds/Supplier/records/S-1001/history"
    past, pinned = await _past_and_pinned_get(http, seeded, path, values="true")
    assert _content(past) == _content(pinned)
    assert [item["revision"] for item in past["items"]] == [seeded.revisions["data"]]
    head = await _get(http, path)
    assert [item["revision"] for item in head["items"]] == [
        seeded.revisions["updated"],
        seeded.revisions["data"],
    ]


async def test_health_context_and_explain(http: httpx.AsyncClient, seeded: Seeded) -> None:
    past, pinned = await _past_and_pinned_get(http, seeded, "/health")
    assert _content(past) == _content(pinned)
    past, pinned = await _past_and_pinned_get(http, seeded, "/kinds/Supplier/health")
    assert _content(past) == _content(pinned)

    pack = {"refs": ["Supplier/S-1001"], "depth": 1, "include_recent_changes": False}
    past, pinned = await _past_and_pinned_post(http, seeded, "/context", pack)
    assert _content(past) == _content(pinned)
    assert "Kavsen Alloys" in str(past)

    explained = await _get(
        http, "/kinds/Supplier/records/S-1001/explain", at=seeded.revisions["data"]
    )
    reference = await _get(http, "/kinds/Supplier/records/S-1001/explain", branch=PINNED)
    assert _content(explained) == _content(reference)


async def _clear_snapshots(seeded: Seeded) -> None:
    async with owner_session(seeded.owner_url) as session, session.begin():
        await session.execute(
            text("DELETE FROM refs WHERE domain_id = :d AND kind = 'snapshot'"), {"d": DOMAIN}
        )


def _time_travel(**settings: Any) -> dict[str, Any]:
    return {"time_travel": settings}


async def test_snapshots_are_shared_hidden_and_dropped_when_unused(
    http: httpx.AsyncClient, seeded: Seeded
) -> None:
    await _clear_snapshots(seeded)
    data = f"~snapshot/{seeded.revisions['data']}"
    where = '{"attr":"country","op":"eq","value":"PL"}'
    await _get(http, "/kinds/Supplier/records", at=seeded.revisions["data"], where=where)
    await _get(http, "/kinds/Supplier/records", at=seeded.revisions["data"], where=where)
    assert await _snapshots(seeded) == [data]
    branches = await _get(http, "/branches")
    assert all(not b["name"].startswith("~") for b in branches["items"])
    unknown = await http.get(f"{BASE}/kinds/Supplier/records/S-1001", params={"at": data})
    assert unknown.status_code == 404
    tag = await http.post(f"{BASE}/tags", json={"name": "from-snapshot", "source": data})
    assert tag.status_code == 404, tag.text

    # a recent snapshot is not dropped for another one: the Domain is full
    full = _time_travel(snapshot_max_per_domain=1, snapshot_min_age_s=3600)
    async with keeper(seeded.app_url, read=full) as one:
        busy = await one.post(f"{BASE}/query", json={**TIER_ONE, "at": seeded.revisions["schema"]})
        assert busy.status_code == 503, busy.text
        assert int(busy.headers["Retry-After"]) >= 1
    assert await _snapshots(seeded) == [data]

    # past its grace period the unused one goes
    replaced = _time_travel(snapshot_max_per_domain=1, snapshot_min_age_s=0)
    async with keeper(seeded.app_url, read=replaced) as one:
        read = await one.post(f"{BASE}/query", json={**TIER_ONE, "at": seeded.revisions["schema"]})
        assert read.status_code == 200, read.text
        assert read.json()["items"] == []
    assert await _snapshots(seeded) == [f"~snapshot/{seeded.revisions['schema']}"]


async def test_snapshot_builds_are_rate_limited_per_principal(seeded: Seeded) -> None:
    await _clear_snapshots(seeded)
    limited = _time_travel(snapshot_builds_per_minute=0.01, snapshot_build_burst=1)
    async with keeper(seeded.app_url, read=limited) as http:
        body = {**TIER_ONE, "at": seeded.revisions["data"]}
        assert (await http.post(f"{BASE}/query", json=body)).status_code == 200
        assert (await http.post(f"{BASE}/query", json=body)).status_code == 200  # no build
        second = await http.post(
            f"{BASE}/query", json={**TIER_ONE, "at": seeded.revisions["updated"]}
        )
        assert second.status_code == 429, second.text
        assert int(second.headers["Retry-After"]) >= 1


async def test_requests_are_validated_before_a_snapshot_is_built(
    http: httpx.AsyncClient, seeded: Seeded
) -> None:
    await _clear_snapshots(seeded)
    at = seeded.revisions["updated"]
    start = {"kind": "Part", "key": "P-10001"}
    bad_hop = await http.post(
        f"{BASE}/traverse", json={"start": start, "path": [{"relation": "nope"}], "at": at}
    )
    assert bad_hop.status_code == 422
    two_kinds = {"q": "x", "kinds": ["Supplier", "Part"], "where": TIER_ONE["where"], "at": at}
    assert (await http.post(f"{BASE}/search", json=two_kinds)).status_code == 422
    hood = {"seeds": [{"kind": "Nope", "key": "x"}], "at": at}
    assert (await http.post(f"{BASE}/neighborhood", json=hood)).status_code == 404
    assert await _snapshots(seeded) == []


async def test_snapshot_names_are_reserved(seeded: Seeded) -> None:
    permissive = {"branches": {"name_pattern": "^.+$"}}
    async with keeper(seeded.app_url, permissive) as http:
        response = await http.post(f"{BASE}/branches", json={"name": "~snapshot/abc"})
        assert response.status_code == 422, response.text


async def test_snapshot_cap_refuses_large_revisions(seeded: Seeded) -> None:
    capped = {"time_travel": {"snapshot_max_entries": 10}}
    async with keeper(seeded.app_url, read=capped) as http:
        at = seeded.revisions["data"]
        refused = await http.post(
            f"{BASE}/query",
            json={**TIER_ONE, "at": at},
        )
        assert refused.status_code == 422
        error = refused.json()["errors"][0]
        assert error["code"] == "time_travel"
        assert "snapshot_max_entries" in refused.json()["detail"]
        search = await http.post(f"{BASE}/search", json={"q": "Kavsen", "at": at})
        assert search.status_code == 422
        # key reads and listings ordered by key still read the tree
        assert (
            await http.get(f"{BASE}/kinds/Supplier/records/S-1001", params={"at": at})
        ).is_success
        assert (await http.get(f"{BASE}/kinds/Supplier/records", params={"at": at})).is_success
    deep = _time_travel(snapshot_max_history_revisions=1)
    async with keeper(seeded.app_url, read=deep) as http:
        refused = await http.post(f"{BASE}/query", json={**TIER_ONE, "at": at})
        assert refused.status_code == 422
        assert "snapshot_max_history_revisions" in refused.json()["detail"]
    async with keeper(seeded.app_url, read=_time_travel(snapshot_max_entries=0)) as http:
        refused = await http.post(f"{BASE}/query", json={**TIER_ONE, "at": at})
        assert refused.status_code == 422
        # at the head nothing needs a snapshot
        head = await http.post(
            f"{BASE}/query",
            json=TIER_ONE,
        )
        assert head.status_code == 200
