"""Per-Record history and lineage against the `supply` example in Postgres."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.validate import RecordDelete
from lorebase_keeper.reading.record_history import RecordHistoryOut, RecordLineageOut
from lorebase_store.runtime import StoreRuntime

from .read_fixtures import Seeded, keeper, seeded_database
from .supply import DOMAIN, Clock, commit_changes, load_supply, record_change, rows

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
HISTORY = f"{BASE}/kinds/Supplier/records/S-1001/history"
LINEAGE = f"{BASE}/kinds/Supplier/records/S-1001/lineage"


async def seed(
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema, (_, _, data) = await load_supply(session, runtime, clock)
    supplier = schema.kind_by_api_name("Supplier")
    assert supplier is not None
    original = next(r for r in rows("supplier.csv") if r["supplier_code"] == "S-1001")
    risk = await commit_changes(
        session,
        runtime,
        clock,
        [record_change(supplier, {**original, "risk_score": "90"})],
        message="Raise the risk of S-1001",
    )
    renamed = await commit_changes(
        session,
        runtime,
        clock,
        [record_change(supplier, {**original, "risk_score": "90", "legal_name": "Renamed"})],
        message="Rename S-1001",
        author="usr_other",
    )
    spare = {
        **original,
        "supplier_code": "S-9999",
        "legal_name": "Spare supplier",
        "vat_number": "PL999999999",
    }
    created = await commit_changes(
        session, runtime, clock, [record_change(supplier, spare)], message="Add S-9999"
    )
    deleted = await commit_changes(
        session,
        runtime,
        clock,
        [RecordDelete(kind=supplier.id, key="S-9999")],
        message="Remove S-9999",
    )
    return Seeded(
        owner_url=owner_url,
        app_url=app_url,
        schema=schema,
        revisions={
            "data": data.revision_id.hex(),
            "risk": risk.revision_id.hex(),
            "renamed": renamed.revision_id.hex(),
            "created": created.revision_id.hex(),
            "deleted": deleted.revision_id.hex(),
        },
    )


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


@pytest.fixture
async def http(seeded: Seeded) -> Any:
    async with keeper(seeded.app_url) as client:
        yield client


async def test_history_lists_changes_newest_first(http: httpx.AsyncClient, seeded: Seeded) -> None:
    response = await http.get(HISTORY)
    assert response.status_code == 200, response.text
    page = RecordHistoryOut.model_validate(response.json())
    revisions = seeded.revisions
    assert page.revision == revisions["deleted"]
    assert [c.revision for c in page.items] == [
        revisions["renamed"],
        revisions["risk"],
        revisions["data"],
    ]
    assert [c.change for c in page.items] == ["updated", "updated", "created"]
    renamed, risk, created = page.items
    assert renamed.author == "usr_other"
    assert renamed.message == "Rename S-1001"
    assert [a.name for a in renamed.attributes] == ["legal_name"]
    assert renamed.attributes[0].before is None  # values only on request
    assert [a.name for a in risk.attributes] == ["risk_score"]
    provenance = risk.attributes[0].provenance
    assert provenance is not None
    assert provenance.origin_type == "operation"
    assert provenance.actor == "usr_editor"
    assert created.attributes[0].name == "supplier_code"
    assert created.attributes[0].provenance is not None
    assert created.attributes[0].provenance.origin_type == "import"
    assert page.next_cursor is None

    again = await http.get(HISTORY, headers={"If-None-Match": response.headers["ETag"]})
    assert again.status_code == 304


async def test_history_values_and_pages(http: httpx.AsyncClient, seeded: Seeded) -> None:
    first = await http.get(HISTORY, params={"values": "true", "limit": 2})
    assert first.status_code == 200, first.text
    body = first.json()
    assert len(body["items"]) == 2
    change = body["items"][0]["attributes"][0]
    assert change["before"] != change["after"]
    assert change["after"] == "Renamed"
    assert body["next_cursor"]
    second = await http.get(
        HISTORY, params={"values": "true", "limit": 2, "cursor": body["next_cursor"]}
    )
    assert second.status_code == 200, second.text
    assert [c["revision"] for c in second.json()["items"]] == [seeded.revisions["data"]]
    assert second.json()["next_cursor"] is None

    # a cursor issued for another request is refused
    other = await http.get(HISTORY, params={"limit": 2, "cursor": body["next_cursor"]})
    assert other.status_code == 422
    assert (await http.get(HISTORY, params={"limit": 10_000})).status_code == 422


async def test_history_of_a_deleted_record(http: httpx.AsyncClient, seeded: Seeded) -> None:
    response = await http.get(
        f"{BASE}/kinds/Supplier/records/S-9999/history", params={"values": "true"}
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [c["change"] for c in items] == ["deleted", "created"]
    assert items[0]["revision"] == seeded.revisions["deleted"]
    names = {a["name"]: a for a in items[0]["attributes"]}
    assert names["legal_name"] == {
        "name": "legal_name",
        "before": "Spare supplier",
        "after": None,
        "provenance": None,
    }
    assert items[0]["provenance"]["origin_type"] == "operation"


async def test_history_unknown(http: httpx.AsyncClient) -> None:
    assert (await http.get(f"{BASE}/kinds/Supplier/records/S-0000/history")).status_code == 404
    assert (await http.get(f"{BASE}/kinds/Nope/records/x/history")).status_code == 404


async def test_lineage_per_attribute(http: httpx.AsyncClient, seeded: Seeded) -> None:
    response = await http.get(LINEAGE)
    assert response.status_code == 200, response.text
    lineage = RecordLineageOut.model_validate(response.json())
    revisions = seeded.revisions
    assert lineage.changes == 3
    assert lineage.created is not None
    assert lineage.created.revision == revisions["data"]
    names = [a.name for a in lineage.attributes]
    assert names[:2] == ["supplier_code", "legal_name"]
    by_name = {a.name: a for a in lineage.attributes}
    assert by_name["legal_name"].value == "Renamed"
    assert by_name["legal_name"].revision == revisions["renamed"]
    assert by_name["legal_name"].author == "usr_other"
    assert by_name["risk_score"].revision == revisions["risk"]
    assert by_name["risk_score"].provenance is not None
    assert by_name["risk_score"].provenance.origin_type == "operation"
    assert by_name["supplier_code"].revision == revisions["data"]
    assert by_name["supplier_code"].provenance is not None
    assert by_name["supplier_code"].provenance.origin_type == "import"
    assert lineage.sources == []
    assert lineage.proposals == []

    again = await http.get(LINEAGE, headers={"If-None-Match": response.headers["ETag"]})
    assert again.status_code == 304
    assert (await http.get(f"{BASE}/kinds/Supplier/records/S-9999/lineage")).status_code == 404


async def test_history_on_an_overlay_branch(http: httpx.AsyncClient, seeded: Seeded) -> None:
    branch = await http.post(
        f"{BASE}/branches",
        json={"name": "history/old", "revision": seeded.revisions["data"], "mode": "overlay"},
    )
    assert branch.status_code == 201, branch.text
    old = await http.get(HISTORY, params={"branch": "history/old"})
    assert old.status_code == 200, old.text
    assert [c["revision"] for c in old.json()["items"]] == [seeded.revisions["data"]]
    lineage = await http.get(LINEAGE, params={"branch": "history/old"})
    assert lineage.status_code == 200, lineage.text
    assert lineage.json()["changes"] == 1
