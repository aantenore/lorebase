"""Stored Operations written in CEL, executed through the API on the `supply` example."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.materialize import create_branch
from lorebase_store.runtime import StoreRuntime

from .read_fixtures import Seeded, keeper, seeded_database
from .supply import DOMAIN, Clock, load_supply

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
BRANCH = "work"

PHASE_OUT = {
    "description": "Phase out a part that more than one supplier can supply.",
    "parameters": [{"name": "part", "type": "ref(Part)", "required": True}],
    "preconditions": [
        {
            "expression": 'size(links(part, "supplied_by")) > 1',
            "message": "Single-sourced parts cannot be phased out.",
        },
        {
            "expression": 'links(part, "supplied_by").all(s, s.status != "terminated")',
            "message": "A supplier of the part is terminated.",
        },
    ],
    "steps": [
        {
            "step": "update_record",
            "kind": "Part",
            "target": "params.part",
            "values": {"lifecycle": '"phase_out"'},
        }
    ],
}


async def seed(
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema, _ = await load_supply(session, runtime, clock)
    await create_branch(
        session, DOMAIN, BRANCH, created_by="usr_editor", runtime=runtime, clock=clock
    )
    return Seeded(owner_url=owner_url, app_url=app_url, schema=schema)


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


async def run(http: Any, operation: str, parameters: dict[str, Any], **extra: Any) -> Any:
    return await http.post(
        f"{BASE}/operations/{operation}/execute",
        json={"branch": BRANCH, "parameters": parameters, **extra},
    )


async def record(http: Any, kind: str, key: str) -> dict[str, Any]:
    found = await http.get(f"{BASE}/kinds/{kind}/records/{key}", params={"branch": BRANCH})
    assert found.status_code == 200, found.text
    values: dict[str, Any] = found.json()["record"]["values"]
    return values


async def test_flag_supplier_risk(seeded: Seeded) -> None:
    async with keeper(seeded.app_url) as http:
        flagged = await run(
            http,
            "flag_supplier_risk",
            {"supplier": "S-1001", "severity": "high", "reason": "Two late deliveries"},
        )
        assert flagged.status_code == 200, flagged.text
        body = flagged.json()
        assert body["status"] == "committed"
        created, linked = body["edits"]
        assert (created["kind"], linked["relation"]) == ("RiskFlag", "flags")
        assert (linked["from"], linked["to"]) == (created["key"], "S-1001")
        values = await record(http, "RiskFlag", created["key"])
        assert values["severity"] == "high"
        assert values["raised_by"] == "anonymous"

        critical = await run(
            http,
            "flag_supplier_risk",
            {"supplier": "S-1001", "severity": "critical", "reason": "Plant fire"},
        )
        assert critical.status_code == 409
        assert critical.json()["code"] == "review-required"
        assert critical.json()["proposals"] == f"{BASE}/proposals"

        terminated = await run(
            http,
            "flag_supplier_risk",
            {"supplier": "S-1019", "severity": "low", "reason": "Still invoicing"},
        )
        assert terminated.status_code == 422
        problem = terminated.json()
        assert problem["code"] == "precondition-failed"
        [issue] = problem["issues"]
        assert issue["message"] == "Terminated suppliers cannot be flagged."
        assert issue["path"] == ["preconditions", 0]
        assert issue["details"] == {
            "expression": 'record(supplier).status != "terminated"',
            "line": 1,
            "column": 1,
        }

        dry = await run(
            http,
            "flag_supplier_risk",
            {"supplier": "S-1019", "severity": "low", "reason": "Still invoicing"},
            dry_run=True,
        )
        assert dry.status_code == 200
        assert dry.json()["violations"][0]["code"] == "operation.precondition_failed"

        missing = await run(
            http,
            "flag_supplier_risk",
            {"supplier": "S-0000", "severity": "low", "reason": "Unknown"},
        )
        assert missing.status_code == 422
        [issue] = missing.json()["issues"]
        assert issue["code"] == "record.not_found"
        assert issue["details"]["column"] >= 1


async def test_approve_contract(seeded: Seeded) -> None:
    async with keeper(seeded.app_url) as http:
        approved = await run(http, "approve_contract", {"contract": "C-3015"})
        assert approved.status_code == 200, approved.text
        values = await record(http, "Contract", "C-3015")
        assert values["status"] == "active"
        assert values["approved_by"] == "anonymous"
        assert "approved_at" in values

        again = await run(http, "approve_contract", {"contract": "C-3015"})
        assert again.status_code == 422
        assert again.json()["code"] == "precondition-failed"
        assert again.json()["issues"][0]["message"] == "Only draft contracts can be approved."

        large = await run(http, "approve_contract", {"contract": "C-3003"})
        assert large.status_code == 409  # annual_value > 500000 needs review
        assert large.json()["code"] == "review-required"
        preview = await run(http, "approve_contract", {"contract": "C-3003"}, dry_run=True)
        assert preview.status_code == 200
        assert preview.json()["review_required"] is True
        assert (await record(http, "Contract", "C-3003"))["status"] == "draft"


async def test_links_in_preconditions(seeded: Seeded) -> None:
    async with keeper(seeded.app_url) as http:
        added = await http.post(
            f"{BASE}/branches/{BRANCH}/schema/changes",
            json={"patch": {"operations": {"phase_out_part": PHASE_OUT}}},
        )
        assert added.status_code == 200, added.text

        single = await run(http, "phase_out_part", {"part": "P-10001"})
        assert single.status_code == 422
        assert single.json()["issues"][0]["message"] == (
            "Single-sourced parts cannot be phased out."
        )
        multi = await run(http, "phase_out_part", {"part": "P-10028"})
        assert multi.status_code == 200, multi.text
        assert (await record(http, "Part", "P-10028"))["lifecycle"] == "phase_out"
