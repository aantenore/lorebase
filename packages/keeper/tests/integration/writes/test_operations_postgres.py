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
    """The example schema, CEL Operations and Derivations included."""
    return (EXAMPLE / "schema.yaml").read_text()


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

        # removing a Relation that holds Links needs cascade (its Derivations go too)
        part = yaml.safe_load(supply_yaml())["kinds"]["Part"]
        del part["attributes"]["supplier_count"]
        del part["attributes"]["single_sourced"]
        removal = {
            "patch": {"kinds": {"Part": part}},
            "remove": [
                {"section": "relations", "name": "supplied_by"},
                {"section": "derivations", "name": "supplier_count"},
                {"section": "derivations", "name": "single_sourced"},
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


def link_targets(relation: str) -> dict[str, list[str]]:
    """`to` keys of an example Link file by `from` key."""
    targets: dict[str, list[str]] = {}
    for row in csv_rows(relation, 10_000):
        targets.setdefault(row["from_key"], []).append(row["to_key"])
    return targets


def with_links(rows: list[dict[str, Any]], key: str, *relations: str) -> list[dict[str, Any]]:
    """Rows with a `links` member naming the Link files' targets of each row."""
    targets = {relation: link_targets(relation) for relation in relations}
    return [
        {
            **row,
            "links": {r: targets[r][row[key]] for r in relations if row[key] in targets[r]},
        }
        for row in rows
    ]


def link_rows(relation: str, *, named: bool = False) -> list[dict[str, Any]]:
    rows = [
        {"from": r.pop("from_key"), "to": r.pop("to_key"), **r} for r in csv_rows(relation, 10_000)
    ]
    return [{"relation": relation, **r} for r in rows] if named else rows


async def test_import_the_supply_example_with_required_links(
    keeper: Any, installation_admin: Any, role_key: Any, database_url: str
) -> None:
    admin = installation_admin.headers
    async with keeper(operations={"import_batch_rows": 7}) as http:
        assert (
            await http.post(f"{API}/domains", json={"id": "supply"}, headers=admin)
        ).status_code == 201
        modeler = await role_key(http, "supply", "modeler")
        editor = await role_key(http, "supply", "editor")
        viewer = await role_key(http, "supply", "viewer")
        await http.post(f"{DOMAIN}/branches", json={"name": "load"}, headers=editor)
        applied = await http.post(
            f"{DOMAIN}/branches/load/schema/changes", json={"yaml": supply_yaml()}, headers=modeler
        )
        assert applied.status_code == 200, applied.text

        async def load(operation: str, parameters: dict[str, Any]) -> Any:
            loaded = await http.post(
                execute(operation),
                json={"branch": "load", "parameters": parameters},
                headers=editor,
            )
            assert loaded.status_code == 200, loaded.text
            return loaded.json()

        for kind, file in (("Supplier", "supplier"), ("Part", "part"), ("Plant", "plant")):
            await load("import_records", {"kind": kind, "rows": csv_rows(file, 10_000)})

        # a Contract without its required with_supplier Link is refused
        [contract] = csv_rows("contract", 1)
        refused = await http.post(
            execute("import_records"),
            json={"branch": "load", "parameters": {"kind": "Contract", "rows": [contract]}},
            headers=editor,
        )
        assert refused.status_code == 422
        assert refused.json()["issues"][0]["relation"]

        contracts = with_links(csv_rows("contract", 10_000), "contract_number", "with_supplier")
        loaded = await load("import_records", {"kind": "Contract", "rows": contracts})
        assert loaded["edit_count"] == 2 * len(contracts)
        orders = with_links(
            csv_rows("purchase_order", 10_000), "po_number", "for_part", "delivered_to"
        )
        loaded = await load("import_records", {"kind": "PurchaseOrder", "rows": orders})
        assert len(loaded["revisions"]) > 1  # batches of whole Records with their Links

        flags = [{"kind": "RiskFlag", **row} for row in csv_rows("risk_flag", 10_000)]
        batch = await load(
            "import_batch",
            {
                "records": flags,
                "links": [
                    *link_rows("flags", named=True),
                    *link_rows("supplied_by", named=True),
                    *link_rows("stocked_at", named=True),
                ],
            },
        )
        assert batch["edit_count"] == len(flags) + sum(
            len(csv_rows(name, 10_000)) for name in ("flags", "supplied_by", "stocked_at")
        )

        for kind, file in (
            ("Supplier", "supplier"),
            ("Part", "part"),
            ("Plant", "plant"),
            ("Contract", "contract"),
            ("PurchaseOrder", "purchase_order"),
            ("RiskFlag", "risk_flag"),
        ):
            page = await http.post(
                f"{DOMAIN}/query",
                json={"branch": "load", "kind": kind, "limit": 500},
                headers=viewer,
            )
            assert page.status_code == 200, page.text
            assert len(page.json()["items"]) == len(csv_rows(file, 10_000)), kind

    engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            report = await verify(
                session, "supply", "load", runtime=StoreRuntime.from_settings(StoreSettings())
            )
        assert report.ok, report
    finally:
        await engine.dispose()


async def test_delete_records_removes_many_records_in_one_revision(
    keeper: Any, installation_admin: Any, role_key: Any, database_url: str
) -> None:
    admin = installation_admin.headers
    async with keeper(operations={"max_bulk_delete_keys": 4}) as http:
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
        suppliers = csv_rows("supplier")
        parts = csv_rows("part")
        for kind, rows in (("Supplier", suppliers), ("Part", parts)):
            loaded = await http.post(
                execute("import_records"),
                json={"branch": "load", "parameters": {"kind": kind, "rows": rows}},
                headers=editor,
            )
            assert loaded.status_code == 200, loaded.text
        codes = [s["supplier_code"] for s in suppliers]
        part_numbers = [p["part_number"] for p in parts]
        # cascade Links (supplied_by's default on_delete) go with the deleted Suppliers
        links = [
            {"from": part_numbers[0], "to": codes[0]},
            {"from": part_numbers[1], "to": codes[1]},
        ]
        linked = await http.post(
            execute("import_links"),
            json={"branch": "load", "parameters": {"relation": "supplied_by", "rows": links}},
            headers=editor,
        )
        assert linked.status_code == 200, linked.text

        def body(keys: list[str], **extra: Any) -> dict[str, Any]:
            return {"branch": "load", "parameters": {"kind": "Supplier", "keys": keys}, **extra}

        head = (await http.get(f"{DOMAIN}/branches/load", headers=viewer)).json()["head"]
        dry = await http.post(
            execute("delete_records"), json=body(codes[:3], dry_run=True), headers=editor
        )
        assert dry.status_code == 200, dry.text
        assert dry.json()["status"] == "dry_run"
        assert dry.json()["edit_count"] == 3
        assert (await http.get(f"{DOMAIN}/branches/load", headers=viewer)).json()["head"] == head

        missing = await http.post(
            execute("delete_records"), json=body([codes[0], "S-0000"]), headers=editor
        )
        assert missing.status_code == 422, missing.text
        assert missing.json()["issues"][0]["code"] == "record.not_found"
        too_many = await http.post(
            execute("delete_records"), json=body([*codes[:4], "S-0000"]), headers=editor
        )
        assert too_many.status_code == 422
        assert too_many.json()["issues"][0]["code"] == "parameter.limit_exceeded"
        stale = await http.post(
            execute("delete_records"), json=body(codes[:3], expected_head="0" * 64), headers=editor
        )
        assert stale.status_code == 409
        denied = await http.post(execute("delete_records"), json=body(codes[:3]), headers=viewer)
        assert denied.status_code == 403

        keyed = {**editor, "Idempotency-Key": "reset-1"}
        reset = body([*codes[:3], "S-0000"])
        reset["parameters"]["skip_missing"] = True
        deleted = await http.post(execute("delete_records"), json=reset, headers=keyed)
        assert deleted.status_code == 200, deleted.text
        assert len(deleted.json()["revisions"]) == 1  # one Revision, cascaded Links included
        assert [w["key"] for w in deleted.json()["warnings"]] == ["S-0000"]
        assert deleted.json()["edit_count"] == 3
        replayed = await http.post(execute("delete_records"), json=reset, headers=keyed)
        assert replayed.json()["replayed"] is True
        for code in codes[:3]:
            gone = await http.get(
                f"{DOMAIN}/kinds/Supplier/records/{code}", params={"branch": "load"}, headers=viewer
            )
            assert gone.status_code == 404
        kept = await http.get(
            f"{DOMAIN}/kinds/Supplier/records/{codes[3]}", params={"branch": "load"}, headers=viewer
        )
        assert kept.status_code == 200

        # main is protected: the bulk delete goes through a Proposal like any built-in
        review = await http.post(
            execute("delete_records"),
            json={"parameters": {"kind": "Supplier", "keys": codes[:1]}},
            headers=editor,
        )
        assert review.status_code == 409
        assert review.json()["code"] == "review-required"

    engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            report = await verify(
                session, "supply", "load", runtime=StoreRuntime.from_settings(StoreSettings())
            )
        assert report.ok, report
    finally:
        await engine.dispose()
