"""Schema reads and change sets through the API against Postgres."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.integration

API = "/api/v1"
DOMAIN = f"{API}/domains/supply"
SUPPLY_SCHEMA = Path(__file__).resolve().parents[5] / "examples" / "supply" / "schema.yaml"


def supply_yaml() -> str:
    """The example schema, CEL Operations and Derivations included."""
    return SUPPLY_SCHEMA.read_text()


async def test_schema_change_sets(keeper: Any, installation_admin: Any, role_key: Any) -> None:
    admin = installation_admin.headers
    async with keeper() as http:
        assert (
            await http.post(f"{API}/domains", json={"id": "supply"}, headers=admin)
        ).status_code == 201
        modeler = await role_key(http, "supply", "modeler")
        editor = await role_key(http, "supply", "editor")
        await http.post(f"{DOMAIN}/branches", json={"name": "model/v1"}, headers=editor)
        changes = f"{DOMAIN}/branches/model/v1/schema/changes"

        denied = await http.post(changes, json={"yaml": supply_yaml()}, headers=editor)
        assert denied.status_code == 403
        assert denied.json()["permission"] == "schema.write"

        protected = await http.post(
            f"{DOMAIN}/branches/main/schema/changes", json={"yaml": supply_yaml()}, headers=modeler
        )
        assert protected.status_code == 409
        assert protected.json()["code"] == "protected-branch"
        assert protected.json()["proposals"] == f"{DOMAIN}/proposals"

        broken: dict[str, Any] = yaml.safe_load(supply_yaml())
        broken["operations"]["approve_contract"]["preconditions"][0]["expression"] = (
            'record(contract).status == "signed"'
        )
        refused = await http.post(changes, json={"yaml": yaml.safe_dump(broken)}, headers=modeler)
        assert refused.status_code == 422
        (issue,) = refused.json()["issues"]
        assert issue["code"] == "schema.expression_invalid"
        assert issue["path"][:2] == ["operations", "approve_contract"]

        dry = await http.post(
            changes, json={"yaml": supply_yaml(), "dry_run": True}, headers=modeler
        )
        assert dry.status_code == 200, dry.text
        assert dry.json()["status"] == "dry_run"
        assert dry.json()["revision"] is None
        assert dry.json()["change_count"] > 10

        head = (await http.get(f"{DOMAIN}/branches/model/v1", headers=modeler)).json()["head"]
        stale = await http.post(
            changes, json={"yaml": supply_yaml(), "expected_head": "0" * 64}, headers=modeler
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "stale-head"
        assert stale.json()["current_head"] == head

        applied = await http.post(
            changes,
            content=supply_yaml(),
            params={"message": "Supply schema"},
            headers={**modeler, "Content-Type": "text/yaml"},
        )
        assert applied.status_code == 200, applied.text
        body = applied.json()
        assert body["status"] == "committed"
        assert body["head"] == head
        created = {(c["section"], c["api_name"]) for c in body["changes"]}
        assert {
            ("kinds", "Supplier"),
            ("kinds", "PurchaseOrder"),
            ("relations", "flags"),
        } <= created

        again = await http.post(changes, json={"yaml": supply_yaml()}, headers=modeler)
        assert again.json()["status"] == "no_op"

        schema = await http.get(f"{DOMAIN}/schema", params={"branch": "model/v1"}, headers=modeler)
        assert schema.headers["lorebase-revision"] == body["revision"]
        kinds = schema.json()["schema"]["kinds"]
        assert "knd_01KEZF7C007ZDX8SK1DZP7MXFF" in kinds
        as_yaml = await http.get(
            f"{DOMAIN}/schema", params={"branch": "model/v1", "format": "yaml"}, headers=modeler
        )
        assert as_yaml.headers["content-type"].startswith("text/yaml")
        assert "Supplier:" in as_yaml.text
        old = await http.get(f"{DOMAIN}/schema", params={"at": head}, headers=modeler)
        assert old.json()["schema"].get("kinds", {}) == {}
        supplier = await http.get(
            f"{DOMAIN}/schema/kinds/Supplier", params={"branch": "model/v1"}, headers=modeler
        )
        assert supplier.json()["api_name"] == "Supplier"

        patch = {
            "kinds": {"Plant": {"description": "Where parts go.", "primary_key": ["plant_code"]}}
        }
        checked = await http.post(
            f"{API}/domains/supply/schema:validate",
            json={"patch": patch, "branch": "model/v1"},
            headers=modeler,
        )
        assert checked.status_code == 200
        assert checked.json()["valid"] is False  # the patch replaces Plant without attributes

        removal = await http.post(
            changes,
            json={"remove": [{"section": "relations", "name": "with_supplier"}]},
            headers=modeler,
        )
        assert removal.json()["status"] == "committed"
        assert removal.json()["changes"][0]["op"] == "delete"


async def test_schema_expressions_can_be_disabled(
    keeper: Any, installation_admin: Any, role_key: Any
) -> None:
    async with keeper(sections={"schema": {"expressions_enabled": False}}) as http:
        assert (
            await http.post(
                f"{API}/domains", json={"id": "supply"}, headers=installation_admin.headers
            )
        ).status_code == 201
        modeler = await role_key(http, "supply", "modeler")
        editor = await role_key(http, "supply", "editor")
        await http.post(f"{DOMAIN}/branches", json={"name": "model"}, headers=editor)
        refused = await http.post(
            f"{DOMAIN}/branches/model/schema/changes",
            json={"yaml": supply_yaml(), "dry_run": True},
            headers=modeler,
        )
        assert refused.status_code == 422
        assert {i["code"] for i in refused.json()["issues"]} == {"schema.expressions_disabled"}
