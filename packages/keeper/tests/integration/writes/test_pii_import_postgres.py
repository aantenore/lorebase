"""Feeding values of vault Attributes through imports and Sources needs `pii.import` (design
13.5): the tombstone check of those commits would otherwise tell any caller whether a guessed
value was erased. The refusal comes before anything is committed, dry runs included."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from .conftest import PEPPER, InstallationAdmin

pytestmark = pytest.mark.integration

API = "/api/v1"
DOMAIN = f"{API}/domains/supply"
SUPPLY_SCHEMA = Path(__file__).resolve().parents[5] / "examples" / "supply" / "schema.yaml"
BRANCH = "model/contacts"
VAULT = {"token_key": "t" * 40, "encryption_key": "e" * 40, "key_id": "k1"}
VAULT_SOURCE = "contacts.suppliers"
PLAIN_SOURCE = "approved.suppliers"
IMPORTER = "pii-importer"
SUPPLIER = {"supplier_code": "S-9001", "legal_name": "Guessed GmbH", "country": "DE"}


def with_email() -> dict[str, Any]:
    """The supply schema with a vault Attribute `email` on Supplier."""
    document: dict[str, Any] = yaml.safe_load(SUPPLY_SCHEMA.read_text())
    document["kinds"]["Supplier"]["attributes"]["email"] = {
        "description": "Contact email.",
        "type": "string",
        "sensitivity": ["pii"],
        "vault": True,
    }
    return document


def with_vault_source() -> dict[str, Any]:
    """`with_email` plus a Source mapping a column to the vault Attribute."""
    document = with_email()
    document["sources"][VAULT_SOURCE] = {
        "description": "Supplier contacts.",
        "type": "csv",
        "connection": "secret://env/SUPPLY_CONTACTS_CSV",
        "target_kind": "Supplier",
        "key_mapping": ["supplier_code"],
        "attribute_mapping": {"email": {"column": "email"}},
    }
    return document


def _settings(installation_admin: InstallationAdmin) -> dict[str, Any]:
    return {
        "auth": {
            "mode": "api_key",
            "api_keys": {"pepper": PEPPER},
            "installation_admins": [installation_admin.id],
            "authz": {
                "grants_cache_ttl_s": 0,
                "custom_roles": {
                    IMPORTER: [
                        "schema.read",
                        "data.read",
                        "operation.execute",
                        "source.sync",
                        "pii.import",
                    ]
                },
            },
        }
    }


def _refused(response: Any) -> None:
    assert response.status_code == 403, response.text
    assert response.json()["permission"] == "pii.import"


async def _merge_proposal(http: Any, source_ref: str, author: Any, approver: Any) -> str:
    proposal = await http.post(
        f"{DOMAIN}/proposals",
        json={"source_ref": source_ref, "target_ref": "main", "title": source_ref},
        headers=author,
    )
    assert proposal.status_code == 201, proposal.text
    proposal_id: str = proposal.json()["id"]
    reviewed = await http.post(
        f"{DOMAIN}/proposals/{proposal_id}/reviews", json={"verdict": "approve"}, headers=approver
    )
    assert reviewed.status_code == 201, reviewed.text
    return proposal_id


async def _import(http: Any, headers: Any, row: dict[str, Any]) -> Any:
    body = {"parameters": {"kind": "Supplier", "rows": [row]}, "dry_run": True}
    return await http.post(
        f"{DOMAIN}/operations/import_records/execute", json=body, headers=headers
    )


async def test_vault_values_need_pii_import_through_imports_and_sources(
    keeper: Any, installation_admin: InstallationAdmin, role_key: Any
) -> None:
    admin = installation_admin.headers
    async with keeper({"vault": VAULT}, **_settings(installation_admin)) as http:
        created = await http.post(f"{API}/domains", json={"id": "supply"}, headers=admin)
        assert created.status_code == 201, created.text
        modeler = await role_key(http, "supply", "modeler")
        maintainer = await role_key(http, "supply", "maintainer")
        editor = await role_key(http, "supply", "editor")
        importer = await role_key(http, "supply", IMPORTER)
        owner = await role_key(http, "supply", "admin")

        # A vault Attribute without a Source feeding it needs nothing more.
        base = await http.post(f"{DOMAIN}/branches", json={"name": "model/base"}, headers=admin)
        assert base.status_code == 201, base.text
        schema = await http.post(
            f"{DOMAIN}/branches/model/base/schema/changes",
            json={"yaml": yaml.safe_dump(with_email())},
            headers=modeler,
        )
        assert schema.status_code == 200, schema.text
        first = await _merge_proposal(http, "model/base", modeler, owner)
        merged = await http.post(f"{DOMAIN}/proposals/{first}/merge", headers=maintainer)
        assert merged.status_code == 200, merged.text

        # Defining a Source that feeds the vault Attribute needs pii.import, on a branch too.
        branch = await http.post(f"{DOMAIN}/branches", json={"name": BRANCH}, headers=admin)
        assert branch.status_code == 201, branch.text
        changes = f"{DOMAIN}/branches/{BRANCH}/schema/changes"
        source_yaml = {"yaml": yaml.safe_dump(with_vault_source())}
        _refused(await http.post(changes, json={**source_yaml, "dry_run": True}, headers=modeler))
        defined = await http.post(changes, json=source_yaml, headers=owner)
        assert defined.status_code == 200, defined.text
        # ... and so does merging it into the branch the syncs read.
        second = await _merge_proposal(http, BRANCH, modeler, owner)
        _refused(await http.post(f"{DOMAIN}/proposals/{second}/merge", headers=maintainer))
        merged = await http.post(f"{DOMAIN}/proposals/{second}/merge", headers=owner)
        assert merged.status_code == 200, merged.text

        # Triggering the Source needs pii.import; other Sources do not.
        sources = f"{DOMAIN}/sources"
        _refused(await http.post(f"{sources}/{VAULT_SOURCE}/sync", headers=editor))
        _refused(
            await http.post(
                f"{sources}/{VAULT_SOURCE}/sync", json={"dry_run": True}, headers=editor
            )
        )
        plain = await http.post(f"{sources}/{PLAIN_SOURCE}/sync", headers=editor)
        assert plain.status_code == 202, plain.text
        granted = await http.post(f"{sources}/{VAULT_SOURCE}/sync", headers=importer)
        assert granted.status_code == 202, granted.text

        # Imports holding a vault value need pii.import, dry runs included; others do not.
        _refused(await _import(http, editor, {**SUPPLIER, "email": "guess@example.com"}))
        without = await _import(http, editor, {**SUPPLIER, "email": None})
        assert without.status_code == 200, without.text
        for headers in (importer, owner):
            allowed = await _import(http, headers, {**SUPPLIER, "email": "guess@example.com"})
            assert allowed.status_code == 200, allowed.text
