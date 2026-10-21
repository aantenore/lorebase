"""Reads through the API on an overlay branch equal those on a full copy (design 6.11, AC-16).

Both branches start at the same head of `main` and get the same commits; `main` moves after
the fork. Every response is compared after masking what legitimately differs between two
branches: Revision ids, timestamps, provenance ids and the branch names. Keeper connects as a
role subject to row-level security, so the read functions are the `_overlay` variants.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.validate import LinkDelete, RecordDelete
from lorebase_store.materialize import MaterializationMode, create_branch
from lorebase_store.runtime import StoreRuntime

from .read_fixtures import Seeded, keeper, seeded_database
from .supply import (
    DOMAIN,
    Clock,
    commit_changes,
    load_supply,
    record_change,
    rows,
)

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
OVERLAY = "agent/review"
COPY = "copy/review"
BRANCHES = (OVERLAY, COPY)

_MASKS = (
    (re.compile(r"[0-9a-f]{64}"), "<revision>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?"), "<at>"),
    (re.compile(r"prv_[0-9A-Z]{26}"), "<provenance>"),
    (re.compile(re.escape(OVERLAY) + "|" + re.escape(COPY)), "<branch>"),
)


def masked(body: Any) -> Any:
    rendered = json.dumps(body, sort_keys=True)
    for pattern, replacement in _MASKS:
        rendered = pattern.sub(replacement, rendered)
    found = json.loads(rendered)
    return _without(found, {"etag"})


def _without(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        return {k: _without(v, keys) for k, v in value.items() if k not in keys}
    if isinstance(value, list):
        return [_without(v, keys) for v in value]
    return value


async def seed(
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema, _ = await load_supply(session, runtime, clock)
    supplier = schema.kind_by_api_name("Supplier")
    part = schema.kind_by_api_name("Part")
    risk = schema.kind_by_api_name("RiskFlag")
    flags = schema.relation_by_api_name("flags")
    assert supplier is not None
    assert part is not None
    assert risk is not None
    assert flags is not None
    modes = {}
    for name, mode in ((OVERLAY, "overlay"), (COPY, "copy")):
        created = await create_branch(
            session, DOMAIN, name, created_by="usr_editor", mode=mode, runtime=runtime, clock=clock
        )
        modes[name] = created.mode
    assert modes == {OVERLAY: MaterializationMode.OVERLAY, COPY: MaterializationMode.COPY}
    suppliers = {r["supplier_code"]: r for r in rows("supplier.csv")}
    parts = {r["part_number"]: r for r in rows("part.csv")}
    flag = rows("flags.csv")[0]
    # The base moves after the fork.
    await commit_changes(
        session,
        runtime,
        clock,
        [
            record_change(supplier, {**suppliers["S-1001"], "risk_score": "91"}),
            record_change(part, {**parts["P-10001"], "name": "Renamed on main"}),
        ],
        message="Main moves",
    )
    for branch in BRANCHES:
        await commit_changes(
            session,
            runtime,
            clock,
            [
                record_change(supplier, {**suppliers["S-1002"], "risk_score": "77"}),
                record_change(
                    supplier,
                    {
                        "supplier_code": "S-9999",
                        "legal_name": "Overlay Bearings GmbH",
                        "country": "DE",
                        "tier": "2",
                        "status": "active",
                    },
                ),
                record_change(part, {**parts["P-10002"], "name": "Branch bearing housing"}),
                LinkDelete.model_validate(
                    {"relation": flags[0].id, "from": flag["from_key"], "to": flag["to_key"]}
                ),
                RecordDelete(kind=risk.id, key=flag["from_key"]),
            ],
            message="Review changes",
            branch=branch,
        )
    return Seeded(owner_url=owner_url, app_url=app_url, schema=schema, facts={"flag": flag})


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


@pytest.fixture
async def http(seeded: Seeded) -> Any:
    async with keeper(seeded.app_url) as client:
        yield client


async def both(http: httpx.AsyncClient, method: str, path: str, **request: Any) -> list[Any]:
    """The masked responses of one request on the overlay and on the copy."""
    found = []
    for branch in BRANCHES:
        if method == "GET":
            params = {**request.get("params", {}), "branch": branch}
            response = await http.get(BASE + path, params=params)
        else:
            response = await http.post(BASE + path, json={**request["json"], "branch": branch})
        assert response.status_code == 200, response.text
        found.append(masked(response.json()))
    return found


QUERIES: list[dict[str, Any]] = [
    {"kind": "Supplier", "order_by": [{"attr": "key"}], "limit": 200},
    {"kind": "Supplier", "where": {"attr": "risk_score", "op": "gte", "value": 70}, "limit": 100},
    {"kind": "Supplier", "where": {"attr": "country", "op": "eq", "value": "DE"}, "limit": 100},
    {
        "kind": "Part",
        "where": {
            "and": [
                {"via": "supplied_by", "where": {"attr": "country", "op": "in", "value": ["PL"]}},
                {"count": "supplied_by", "op": "gte", "value": 1},
            ]
        },
        "select": ["name"],
        "include": {"supplied_by": {"select": ["country"], "limit": 2}},
        "order_by": [{"attr": "key"}],
        "limit": 100,
        "aggregate": {"group_by": "category"},
    },
    {"kind": "Supplier", "where": {"not": {"via": "supplies"}}, "limit": 100},
    {
        "kind": "Supplier",
        "where": {
            "via": "purchase_orders",
            "where": {"attr": "status", "op": "eq", "value": "shipped"},
        },
        "limit": 100,
    },
    {"trait": "Located", "where": {"attr": "country", "op": "eq", "value": "DE"}, "limit": 100},
    {"kind": "Part", "where": {"match": "bearing"}, "limit": 100},
    {"kind": "RiskFlag", "order_by": [{"attr": "key"}], "limit": 200},
]


async def test_query_compiler_reads_the_overlay_like_a_copy(http: httpx.AsyncClient) -> None:
    for body in QUERIES:
        overlay, copy = await both(http, "POST", "/query", json=body)
        assert overlay == copy, body
        assert overlay["items"], body


async def test_records_links_and_explain_read_the_overlay_like_a_copy(
    http: httpx.AsyncClient, seeded: Seeded
) -> None:
    for kind in ("Supplier", "Part", "RiskFlag"):
        overlay, copy = await both(http, "GET", f"/kinds/{kind}/records", params={"limit": 500})
        assert overlay == copy
    flagged = seeded.facts["flag"]["to_key"]
    for path in (
        "/kinds/Supplier/records/S-1001",
        "/kinds/Supplier/records/S-1002",
        "/kinds/Supplier/records/S-9999",
        f"/kinds/Supplier/records/{flagged}",
        "/kinds/Part/records/P-10001",
        "/kinds/Part/records/P-10002",
    ):
        overlay, copy = await both(http, "GET", path, params={"links": True})
        assert overlay == copy, path
        overlay, copy = await both(http, "GET", path + "/explain")
        assert overlay == copy, path
    overlay, _ = await both(http, "GET", "/kinds/Supplier/records/S-1001")
    assert overlay["record"]["values"]["risk_score"] == "32", "read as of the fork"
    for path in (
        f"/kinds/Supplier/records/{flagged}/links/supplies",
        f"/kinds/Supplier/records/{flagged}/links/contracts",
        "/kinds/Supplier/records/S-1002/links/purchase_orders",
        "/kinds/Part/records/P-10002/links/supplied_by",
    ):
        overlay, copy = await both(http, "GET", path)
        assert overlay == copy, path


async def test_search_neighborhood_and_context_read_the_overlay_like_a_copy(
    http: httpx.AsyncClient,
) -> None:
    for body in (
        {"q": "bearing", "limit": 20},
        {"q": "Overlay", "kinds": ["Supplier"]},
        {
            "q": "seals",
            "kinds": ["Supplier"],
            "where": {"attr": "country", "op": "eq", "value": "PL"},
        },
    ):
        overlay, copy = await both(http, "POST", "/search", json=body)
        assert overlay == copy, body
    neighborhood = {"seeds": [{"kind": "Supplier", "key": "S-1002"}], "depth": 2}
    overlay, copy = await both(http, "POST", "/neighborhood", json=neighborhood)
    assert overlay == copy
    pack = {"refs": ["Supplier/S-1002", "Supplier/S-9999"], "budget_tokens": 32000}
    overlay, copy = await both(http, "POST", "/context", json=pack)
    assert overlay == copy


async def test_health_counts_read_the_overlay_like_a_copy(http: httpx.AsyncClient) -> None:
    overlay, copy = await both(http, "GET", "/health")
    assert overlay == copy
    for kind in ("Supplier", "Part", "RiskFlag"):
        overlay, copy = await both(http, "GET", f"/kinds/{kind}/health")
        assert overlay == copy, kind


async def test_overlay_branch_is_created_through_the_api(http: httpx.AsyncClient) -> None:
    created = await http.post(f"{BASE}/branches", json={"name": "agent/api", "mode": "overlay"})
    assert created.status_code == 201, created.text
    assert created.json()["materialization"] == "overlay"
    defaulted = await http.post(f"{BASE}/branches", json={"name": "extract/api"})
    assert defaulted.json()["materialization"] == "overlay"
    copied = await http.post(f"{BASE}/branches", json={"name": "feature/api"})
    assert copied.json()["materialization"] == "full"
    on_copy = await http.post(
        f"{BASE}/branches", json={"name": "agent/on-feature", "source": "feature/api"}
    )
    assert on_copy.json()["materialization"] == "overlay"
    # Deleting a base promotes the overlays that read it.
    deleted = await http.delete(f"{BASE}/branches/feature%2Fapi")
    assert deleted.status_code in (200, 204), deleted.text
    promoted = await http.get(f"{BASE}/branches/agent%2Fon-feature")
    assert promoted.json()["materialization"] == "full"
    records = await http.get(
        f"{BASE}/kinds/Supplier/records", params={"branch": "agent/on-feature", "limit": 500}
    )
    assert len(records.json()["items"]) == len(rows("supplier.csv"))
