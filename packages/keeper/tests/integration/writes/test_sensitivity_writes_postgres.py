"""Sensitivity tags cannot be shed to read a value in clear (design 10.2): not by removing a
tag on a branch, not by deriving an untagged copy, not by merging such a change."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.integration

API = "/api/v1"
DOMAIN = f"{API}/domains/supply"
SUPPLY_SCHEMA = Path(__file__).resolve().parents[5] / "examples" / "supply" / "schema.yaml"
BRANCH = "model/x"
CHANGES = f"{DOMAIN}/branches/{BRANCH}/schema/changes"


def supply() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(SUPPLY_SCHEMA.read_text())
    return document


def untagged() -> dict[str, Any]:
    """The supply schema with `confidential` removed from Supplier.vat_number."""
    document = supply()
    del document["kinds"]["Supplier"]["attributes"]["vat_number"]["sensitivity"]
    return document


def with_copy(document: dict[str, Any]) -> dict[str, Any]:
    """`document` plus an untagged derived Supplier.vat_copy = vat_number (the exploit)."""
    document["kinds"]["Supplier"]["attributes"]["vat_copy"] = {
        "description": "Copy of the VAT number.",
        "type": "string",
        "derivation": "vat_copy",
    }
    document["derivations"]["vat_copy"] = {
        "description": "Copies the VAT number.",
        "definition": {
            "variant": "expression",
            "kind": "Supplier",
            "attribute": "vat_copy",
            "expression": "vat_number",
        },
    }
    return document


async def change(http: Any, headers: dict[str, str], document: dict[str, Any]) -> Any:
    return await http.post(CHANGES, json={"yaml": yaml.safe_dump(document)}, headers=headers)


async def test_tags_cannot_be_shed_on_a_branch_or_through_a_merge(
    keeper: Any, installation_admin: Any, role_key: Any
) -> None:
    admin = installation_admin.headers
    async with keeper() as http:
        created = await http.post(f"{API}/domains", json={"id": "supply"}, headers=admin)
        assert created.status_code == 201, created.text
        modeler = await role_key(http, "supply", "modeler")
        maintainer = await role_key(http, "supply", "maintainer")
        loaded = await http.post(f"{DOMAIN}/branches", json={"name": "model/base"}, headers=admin)
        assert loaded.status_code == 201, loaded.text
        base = await http.post(
            f"{DOMAIN}/branches/model/base/schema/changes",
            json={"yaml": SUPPLY_SCHEMA.read_text()},
            headers=modeler,
        )
        assert base.status_code == 200, base.text
        proposal = await http.post(
            f"{DOMAIN}/proposals",
            json={"source_ref": "model/base", "target_ref": "main", "title": "Supply schema"},
            headers=modeler,
        )
        assert proposal.status_code == 201, proposal.text
        reviewed = await http.post(
            f"{DOMAIN}/proposals/{proposal.json()['id']}/reviews",
            json={"verdict": "approve"},
            headers=admin,
        )
        assert reviewed.status_code == 201, reviewed.text
        merged = await http.post(f"{DOMAIN}/proposals/{proposal.json()['id']}/merge", headers=admin)
        assert merged.status_code == 200, merged.text
        branch = await http.post(f"{DOMAIN}/branches", json={"name": BRANCH}, headers=admin)
        assert branch.status_code == 201, branch.text

        # Removing the tag on a branch needs the clearance.
        refused = await change(http, modeler, untagged())
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "sensitivity_lowered"
        # An untagged derived copy of the value is refused.
        copied = await change(http, modeler, with_copy(supply()))
        assert copied.status_code == 422, copied.text
        assert {i["code"] for i in copied.json()["issues"]} == {"schema.sensitivity_inherited"}

        # A cleared caller may remove it on the branch; merging that needs the clearance too.
        shed = await change(http, admin, untagged())
        assert shed.status_code == 200, shed.text
        lowering = await http.post(
            f"{DOMAIN}/proposals",
            json={"source_ref": BRANCH, "target_ref": "main", "title": "Untag"},
            headers=modeler,
        )
        assert lowering.status_code == 201, lowering.text
        approve = await http.post(
            f"{DOMAIN}/proposals/{lowering.json()['id']}/reviews",
            json={"verdict": "approve"},
            headers=admin,
        )
        assert approve.status_code == 201, approve.text
        denied = await http.post(
            f"{DOMAIN}/proposals/{lowering.json()['id']}/merge", headers=maintainer
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["code"] == "sensitivity_lowered"
