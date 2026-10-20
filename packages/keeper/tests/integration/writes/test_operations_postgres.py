"""The full write flow through the API against Postgres: Domain, schema, branch, Operations."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from lorebase_store.db import create_engine
from lorebase_store.materialize import verify
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings

pytestmark = pytest.mark.integration

API = "/api/v1"
DOMAIN = f"{API}/domains/supply"
EXAMPLE = Path(__file__).resolve().parents[5] / "examples" / "supply"
ROWS = 5


def supply_yaml() -> str:
    """The example schema without the elements that need expressions (not enabled yet)."""
    document: dict[str, Any] = yaml.safe_load((EXAMPLE / "schema.yaml").read_text())
    del document["operations"]
    del document["derivations"]["single_sourced"]
    del document["kinds"]["Part"]["attributes"]["single_sourced"]
    return yaml.safe_dump(document, sort_keys=False)


def csv_rows(name: str, limit: int = ROWS) -> list[dict[str, Any]]:
    """The first rows of an example CSV as JSON rows: empty cells left out, flags as booleans."""
    with (EXAMPLE / "data" / f"{name}.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))[:limit]
    flags = {"true": True, "false": False}
    return [{k: flags.get(v, v) for k, v in row.items() if v != ""} for row in rows]


def execute(name: str) -> str:
    return f"{DOMAIN}/operations/{name}/execute"


async def _scalar(database_url: str, sql: str) -> Any:
    engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text(sql))).scalar_one()
    finally:
        await engine.dispose()


async def test_operations_end_to_end(
    keeper: Any, installation_admin: Any, role_key: Any, database_url: str
) -> None:
    admin = installation_admin.headers
    async with keeper(operations={"import_batch_rows": 3}) as http:
        assert (
            await http.post(f"{API}/domains", json={"id": "supply"}, headers=admin)
        ).status_code == 201
        modeler = await role_key(http, "supply", "modeler")
        editor = await role_key(http, "supply", "editor")
        viewer = await role_key(http, "supply", "viewer")
        assert (
            await http.post(f"{DOMAIN}/branches", json={"name": "load"}, headers=editor)
        ).status_code == 201
        applied = await http.post(
            f"{DOMAIN}/branches/load/schema/changes", json={"yaml": supply_yaml()}, headers=modeler
        )
        assert applied.status_code == 200, applied.text

        # bulk loads, two Revisions per import with batches of 3 rows
        for kind, rows in (("Supplier", csv_rows("supplier")), ("Part", csv_rows("part"))):
            loaded = await http.post(
                execute("import_records"),
                json={"branch": "load", "parameters": {"kind": kind, "rows": rows}},
                headers=editor,
            )
            assert loaded.status_code == 200, loaded.text
            assert loaded.json()["edit_count"] == ROWS
            assert len(loaded.json()["revisions"]) == 2
        links = [
            {"from": r.pop("from_key"), "to": r.pop("to_key"), **r}
            for r in csv_rows("supplied_by", 20)
            if r["from_key"] in {p["part_number"] for p in csv_rows("part")}
            and r["to_key"] in {s["supplier_code"] for s in csv_rows("supplier")}
        ]
        linked = await http.post(
            execute("import_links"),
            json={"branch": "load", "parameters": {"relation": "supplied_by", "rows": links}},
            headers=editor,
        )
        assert linked.status_code == 200, linked.text

        # definition of a built-in, derived from the schema
        definition = await http.get(
            f"{DOMAIN}/schema/operations/create_supplier", params={"branch": "load"}, headers=viewer
        )
        assert definition.json()["builtin"] is True

        new_supplier = {
            "supplier_code": "S-9001",
            "legal_name": "Orvane Fittings GmbH",
            "country": "DE",
            "city": "Bremen",
        }
        head = (await http.get(f"{DOMAIN}/branches/load", headers=viewer)).json()["head"]
        dry = await http.post(
            execute("create_supplier"),
            json={"branch": "load", "parameters": new_supplier, "dry_run": True},
            headers=editor,
        )
        assert dry.status_code == 200, dry.text
        assert dry.json()["status"] == "dry_run"
        assert dry.json()["revision"] is None
        assert dry.json()["edits"][0]["key"] == "S-9001"
        assert (await http.get(f"{DOMAIN}/branches/load", headers=viewer)).json()["head"] == head

        bad = {**new_supplier, "country": "Germany"}
        dry_bad = await http.post(
            execute("create_supplier"),
            json={"branch": "load", "parameters": bad, "dry_run": True},
            headers=editor,
        )
        assert dry_bad.json()["violation_count"] >= 1
        rejected = await http.post(
            execute("create_supplier"), json={"branch": "load", "parameters": bad}, headers=editor
        )
        assert rejected.status_code == 422
        assert rejected.json()["status"] == "rejected"
        assert rejected.json()["issues"][0]["code"] == "value.pattern"

        stale = await http.post(
            execute("create_supplier"),
            json={"branch": "load", "parameters": new_supplier, "expected_head": "0" * 64},
            headers=editor,
        )
        assert stale.status_code == 409
        assert stale.json()["current_head"] == head

        keyed = {**editor, "Idempotency-Key": "new-supplier-1"}
        created = await http.post(
            execute("create_supplier"),
            json={"branch": "load", "parameters": new_supplier, "message": "Onboard S-9001"},
            headers=keyed,
        )
        assert created.status_code == 200, created.text
        revision = created.json()["revision"]
        assert created.json()["head"] == head
        replayed = await http.post(
            execute("create_supplier"),
            json={"branch": "load", "parameters": new_supplier},
            headers=keyed,
        )
        assert replayed.json()["replayed"] is True
        assert replayed.json()["revision"] == revision
        reused = await http.post(
            execute("create_supplier"),
            json={"branch": "load", "parameters": {**new_supplier, "city": "Kiel"}},
            headers=keyed,
        )
        assert reused.status_code == 409
        assert reused.json()["code"] == "idempotency-conflict"

        updated = await http.post(
            execute("update_supplier"),
            json={"branch": "load", "parameters": {"key": "S-9001", "city": None, "tier": "2"}},
            headers=editor,
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["edits"][0]["values"].get("city") is None
        assert (
            await http.post(
                execute("link_supplied_by"),
                json={"branch": "load", "parameters": {"from": "P-10001", "to": "S-9001"}},
                headers=editor,
            )
        ).status_code == 200
        assert (
            await http.post(
                execute("unlink_supplied_by"),
                json={"branch": "load", "parameters": {"from": "P-10001", "to": "S-9001"}},
                headers=editor,
            )
        ).status_code == 200
        deleted = await http.post(
            execute("delete_supplier"),
            json={"branch": "load", "parameters": {"key": "S-9001"}},
            headers=editor,
        )
        assert deleted.status_code == 200

        denied = await http.post(
            execute("create_supplier"),
            json={"branch": "load", "parameters": new_supplier},
            headers=viewer,
        )
        assert denied.status_code == 403
        assert denied.json()["permission"] == "operation.execute"
        unknown = await http.post(execute("launch_rocket"), json={"branch": "load"}, headers=editor)
        assert unknown.status_code == 404

        # main is protected: built-in Operations go through Proposals
        review = await http.post(
            execute("import_records"),
            json={"parameters": {"kind": "Plant", "rows": csv_rows("plant")}},
            headers=editor,
        )
        assert review.status_code == 409
        assert review.json()["code"] == "review-required"
        assert review.json()["proposals"] == f"{DOMAIN}/proposals"

        # removing a Relation that holds Links needs cascade (its Derivation goes too)
        part = yaml.safe_load(supply_yaml())["kinds"]["Part"]
        del part["attributes"]["supplier_count"]
        removal = {
            "patch": {"kinds": {"Part": part}},
            "remove": [
                {"section": "relations", "name": "supplied_by"},
                {"section": "derivations", "name": "supplier_count"},
            ],
        }
        blocked = await http.post(
            f"{DOMAIN}/branches/load/schema/changes", json=removal, headers=modeler
        )
        assert blocked.status_code == 422, blocked.text
        assert blocked.json()["issues"][0]["path"] == ["relations", "supplied_by"]
        cascaded = await http.post(
            f"{DOMAIN}/branches/load/schema/changes",
            json={**removal, "cascade": True, "dry_run": True},
            headers=modeler,
        )
        assert cascaded.status_code == 200, cascaded.text
        assert cascaded.json()["data_changes"] == len(links)

    invocations = await _scalar(database_url, "SELECT count(*) FROM operation_invocations")
    assert invocations == 8  # 3 imports, create, update, link, unlink, delete
    invoked = await _scalar(
        database_url, "SELECT count(*) FROM audit_log WHERE action = 'operation.invoke'"
    )
    assert invoked == invocations
    origins = await _scalar(
        database_url,
        "SELECT count(DISTINCT origin->>'type') FROM revisions WHERE origin->>'type' IN "
        "('operation', 'import')",
    )
    assert origins == 2
    engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            report = await verify(
                session, "supply", "load", runtime=StoreRuntime.from_settings(StoreSettings())
            )
        assert report.ok, report
    finally:
        await engine.dispose()
