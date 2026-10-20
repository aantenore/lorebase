"""Agent-facing reads against the `supply` example in Postgres: context packs and Record
explanations.

Revision ids, timestamps and values of the seeded Domain are deterministic (fixed clock and
content addressing), so the markdown renderings of Supplier/S-1001 are kept as snapshots in
`snapshots/`; set LOREBASE_UPDATE_SNAPSHOTS=1 to rewrite them after an intended change.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_keeper.agents.explain import ExplainOut
from lorebase_keeper.agents.pack import ContextPackOut
from lorebase_store.runtime import StoreRuntime

from .read_fixtures import Seeded, keeper, seeded_database
from .supply import DOMAIN, Clock, commit_changes, load_supply, record_change, rows

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
SNAPSHOTS = Path(__file__).parent / "snapshots"
UPDATE = os.environ.get("LOREBASE_UPDATE_SNAPSHOTS") == "1"


async def seed(
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema, (_, _, data) = await load_supply(session, runtime, clock)
    supplier = schema.kind_by_api_name("Supplier")
    assert supplier is not None
    original = next(r for r in rows("supplier.csv") if r["supplier_code"] == "S-1001")
    updated = await commit_changes(
        session,
        runtime,
        clock,
        [record_change(supplier, {**original, "risk_score": "90"})],
        message="Raise the risk of S-1001",
    )
    return Seeded(
        owner_url=owner_url,
        app_url=app_url,
        schema=schema,
        revisions={"data": data.revision_id.hex(), "updated": updated.revision_id.hex()},
    )


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


@pytest.fixture
async def http(seeded: Seeded) -> Any:
    async with keeper(seeded.app_url) as client:
        yield client


async def context(http: httpx.AsyncClient, body: dict[str, Any], status: int = 200) -> Any:
    response = await http.post(f"{BASE}/context", json=body)
    assert response.status_code == status, response.text
    return response


def check_snapshot(name: str, rendered: str) -> None:
    path = SNAPSHOTS / name
    if UPDATE or not path.exists():
        path.parent.mkdir(exist_ok=True)
        path.write_text(rendered)
        if not UPDATE:
            pytest.fail(f"snapshot {path.name} was missing and has been written; rerun")
    assert rendered == path.read_text()


async def test_context_pack_of_a_supplier(http: httpx.AsyncClient, seeded: Seeded) -> None:
    response = await context(http, {"refs": ["Supplier/S-1001"], "budget_tokens": 32000})
    pack = response.json()
    ContextPackOut.model_validate(pack)
    head = seeded.revisions["updated"]
    assert pack["revision"] == head
    assert response.headers["Lorebase-Revision"] == head
    assert response.headers["ETag"] == pack["etag"]
    assert pack["etag"].startswith(f'"{head}.')

    (seed_record,) = pack["seeds"]
    assert seed_record["ref"] == "Supplier/S-1001"
    assert seed_record["title"] == "Kavsen Alloys Sp. z o.o."
    assert seed_record["values"]["risk_score"] == "90"

    neighbors = {n["ref"] for n in pack["neighbors"]}
    expected = {f"Part/{r['from_key']}" for r in rows("supplied_by.csv") if r["to_key"] == "S-1001"}
    expected |= {
        f"Contract/{r['from_key']}" for r in rows("with_supplier.csv") if r["to_key"] == "S-1001"
    }
    expected |= {
        f"PurchaseOrder/{r['po_number']}"
        for r in rows("purchase_order.csv")
        if r["supplier"] == "S-1001"
    }
    expected |= {f"RiskFlag/{r['from_key']}" for r in rows("flags.csv") if r["to_key"] == "S-1001"}
    assert neighbors == expected
    assert all(n["depth"] == 1 for n in pack["neighbors"])
    assert {"supplied_by", "with_supplier", "issued_to"} <= set(pack["links"])
    supplied = pack["links"]["supplied_by"]
    assert all(link["to"] == "Supplier/S-1001" for link in supplied)

    kinds = [k["name"] for k in pack["schema"]["kinds"]]
    assert kinds == sorted(kinds)
    assert "Supplier" in kinds
    assert "Plant" not in kinds  # not present in the pack
    risk = [c for c in pack["recent_changes"] if c["ref"] == "Supplier/S-1001"]
    assert risk[-1]["change"] == "modified"
    assert risk[-1]["attributes"] == ["risk_score"]
    assert risk[-1]["revision"] == head
    assert risk[-1]["actor"] == "usr_editor"
    lineage = {tuple(item["attributes"]): item["origin"] for item in pack["lineage"]}
    assert lineage[("risk_score",)] == "operation"
    assert pack["budget"]["used_tokens"] <= pack["budget"]["limit_tokens"]
    assert pack["budget"]["trimmed"] == []

    again = await http.post(
        f"{BASE}/context",
        json={"refs": ["Supplier/S-1001"], "budget_tokens": 32000},
        headers={"If-None-Match": pack["etag"]},
    )
    assert again.status_code == 304
    cached = await context(http, {"refs": ["Supplier/S-1001"], "budget_tokens": 32000})
    assert cached.content == response.content


async def test_markdown_pack_snapshot(http: httpx.AsyncClient) -> None:
    body = {"refs": ["Supplier/S-1001"], "format": "markdown", "budget_tokens": 1500}
    response = await context(http, body)
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    text = response.text
    assert text.startswith("# Context: supply/main\n")
    check_snapshot("context_supplier_s1001.md", text)
    assert (await context(http, body)).text == text


async def test_budget_trims_by_priority(http: httpx.AsyncClient) -> None:
    pack = (await context(http, {"refs": ["Supplier/S-1001"], "budget_tokens": 400})).json()
    budget = pack["budget"]
    steps = [t["step"] for t in budget["trimmed"]]
    assert steps[:2] == ["lineage", "recent_changes"]
    assert budget["used_tokens"] <= 400 or budget["over_budget"]
    assert pack["seeds"][0]["values"]["risk_score"] == "90"


async def test_seeds_from_a_query_and_from_search(http: httpx.AsyncClient) -> None:
    query = {"kind": "Supplier", "where": {"attr": "country", "op": "eq", "value": "PL"}}
    pack = (await context(http, {"query": {**query, "limit": 2}, "depth": 0})).json()
    assert len(pack["seeds"]) == 2
    assert all(s["values"]["country"] == "PL" for s in pack["seeds"])
    assert pack["neighbors"] == []
    assert pack["links"] == {}

    found = (await context(http, {"search": "Kavsen", "depth": 0})).json()
    assert found["seeds"][0]["ref"] == "Supplier/S-1001"


async def test_changes_since_a_revision(http: httpx.AsyncClient, seeded: Seeded) -> None:
    body: dict[str, Any] = {"refs": ["Supplier/S-1001"], "depth": 0}
    after_data = (await context(http, {**body, "since": seeded.revisions["data"]})).json()
    assert after_data["since"] == seeded.revisions["data"]
    assert [c["revision"] for c in after_data["recent_changes"]] == [seeded.revisions["updated"]]
    at_head = (await context(http, {**body, "since": seeded.revisions["updated"]})).json()
    assert at_head["recent_changes"] == []


async def test_invalid_context_requests(http: httpx.AsyncClient) -> None:
    await context(http, {"refs": ["Supplier/S-0000"]}, 404)
    await context(http, {"refs": ["Nope/1"]}, 404)
    await context(http, {"refs": ["Supplier"]}, 422)
    await context(http, {"refs": ["Supplier/S-1001"], "search": "x"}, 422)
    await context(http, {"refs": ["Supplier/S-1001"], "depth": 9}, 422)
    await context(http, {"refs": ["Supplier/S-1001"], "budget_tokens": 10**6}, 422)


# Explain --------------------------------------------------------------------------------------


async def test_explain_a_supplier(http: httpx.AsyncClient, seeded: Seeded) -> None:
    path = f"{BASE}/kinds/Supplier/records/S-1001/explain"
    response = await http.get(path)
    assert response.status_code == 200, response.text
    document = response.json()
    ExplainOut.model_validate(document)
    data, updated = seeded.revisions["data"], seeded.revisions["updated"]
    assert document["revision"] == updated
    assert response.headers["Lorebase-Revision"] == updated

    created = document["created"]
    assert created["revision"] == data
    assert created["actor"] == "usr_loader"
    assert created["origin_type"] == "import"
    assert created["revision_origin"] == {"type": "import", "ref": "examples/supply"}
    assert created["message"] == "Import the supply example data"

    attributes = {a["name"]: a for a in document["attributes"]}
    assert [a["name"] for a in document["attributes"]][:2] == ["supplier_code", "legal_name"]
    assert attributes["risk_score"]["value"] == "90"
    assert attributes["risk_score"]["revision"] == updated
    assert attributes["risk_score"]["actor"] == "usr_editor"
    assert attributes["risk_score"]["origin_type"] == "operation"
    assert attributes["legal_name"]["revision"] == data
    assert attributes["legal_name"]["origin_type"] == "import"

    links = {(link["relation"], link["ref"]): link for link in document["links"]}
    supplies = links[("supplies", "Part/P-10028")]
    assert supplies["direction"] == "inverse"
    assert supplies["revision"] == data
    assert supplies["values"] == {"is_primary": True, "lead_time_days": "21"}
    order = links[("purchase_orders", "PurchaseOrder/PO-200002")]
    assert order["origin_type"] == "import"
    assert order["revision"] == data
    assert ("contracts", "Contract/C-3001") in links
    assert document["links_truncated"] is False
    assert document["history"] == {"changes": 2, "first_revision": data, "last_revision": updated}
    assert document["proposals"] == []

    again = await http.get(path, headers={"If-None-Match": response.headers["ETag"]})
    assert again.status_code == 304


async def test_explain_markdown_snapshot(http: httpx.AsyncClient) -> None:
    path = f"{BASE}/kinds/Supplier/records/S-1001/explain"
    response = await http.get(path, params={"format": "markdown"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    check_snapshot("explain_supplier_s1001.md", response.text)


async def test_explain_unknown_record(http: httpx.AsyncClient) -> None:
    missing = await http.get(f"{BASE}/kinds/Supplier/records/S-0000/explain")
    assert missing.status_code == 404
    assert (await http.get(f"{BASE}/kinds/Nope/records/x/explain")).status_code == 404
