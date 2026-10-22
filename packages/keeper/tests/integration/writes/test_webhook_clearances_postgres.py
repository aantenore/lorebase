"""A webhook is cleared for sensitive values only by a caller holding those clearances on the
whole Domain (design 10.5)."""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import PEPPER

pytestmark = pytest.mark.integration

API = "/api/v1"
HOOKS = f"{API}/domains/acme/webhooks"
SEALED = {"webhooks": {"encryption_key": "webhook-key-0123456789-0123456789-abcdef"}}


async def test_clearances_need_the_matching_permission(
    keeper: Any, installation_admin: Any, role_key: Any
) -> None:
    auth = {
        "mode": "api_key",
        "api_keys": {"pepper": PEPPER},
        "installation_admins": [installation_admin.id],
        "authz": {
            "grants_cache_ttl_s": 0,
            "custom_roles": {
                "hook-admin": ["schema.read", "data.read", "domain.settings"],
                "hook-pii": ["schema.read", "data.read", "domain.settings", "pii.read"],
            },
        },
    }
    async with keeper(SEALED, auth=auth) as http:
        created = await http.post(
            f"{API}/domains", json={"id": "acme"}, headers=installation_admin.headers
        )
        assert created.status_code == 201, created.text
        plain = await role_key(http, "acme", "hook-admin")
        cleared = await role_key(http, "acme", "hook-pii")
        url = "https://hooks.example.com/in"

        refused = await http.post(HOOKS, json={"url": url, "clearances": ["pii"]}, headers=plain)
        assert refused.status_code == 403, refused.text
        assert refused.json()["permission"] == "pii.read"
        masked = await http.post(HOOKS, json={"url": url}, headers=plain)
        assert masked.status_code == 201, masked.text
        assert masked.json()["clearances"] == []

        allowed = await http.post(HOOKS, json={"url": url, "clearances": ["pii"]}, headers=cleared)
        assert allowed.status_code == 201, allowed.text
        assert allowed.json()["clearances"] == ["pii"]
        beyond = await http.post(
            HOOKS, json={"url": url, "clearances": ["pii", "restricted"]}, headers=cleared
        )
        assert beyond.status_code == 403
        assert beyond.json()["permission"] == "restricted.read"


async def test_clearances_can_be_changed(
    keeper: Any, installation_admin: Any, role_key: Any
) -> None:
    auth = {
        "mode": "api_key",
        "api_keys": {"pepper": PEPPER},
        "installation_admins": [installation_admin.id],
        "authz": {
            "grants_cache_ttl_s": 0,
            "custom_roles": {
                "hook-admin": ["schema.read", "data.read", "domain.settings"],
                "hook-pii": ["schema.read", "data.read", "domain.settings", "pii.read"],
            },
        },
    }
    async with keeper(SEALED, auth=auth) as http:
        created = await http.post(
            f"{API}/domains", json={"id": "acme"}, headers=installation_admin.headers
        )
        assert created.status_code == 201, created.text
        plain = await role_key(http, "acme", "hook-admin")
        cleared = await role_key(http, "acme", "hook-pii")
        made = await http.post(HOOKS, json={"url": "https://hooks.example.com/in"}, headers=plain)
        assert made.status_code == 201, made.text
        one = f"{HOOKS}/{made.json()['id']}"

        refused = await http.patch(one, json={"clearances": ["pii"]}, headers=plain)
        assert refused.status_code == 403, refused.text
        assert refused.json()["permission"] == "pii.read"
        invalid = await http.patch(one, json={"clearances": ["secret"]}, headers=cleared)
        assert invalid.status_code == 422

        added = await http.patch(one, json={"clearances": ["pii"]}, headers=cleared)
        assert added.status_code == 200, added.text
        assert added.json()["clearances"] == ["pii"]
        listed = await http.get(HOOKS, headers=plain)
        assert [item["clearances"] for item in listed.json()["items"]] == [["pii"]]

        unchanged = await http.patch(one, json={"clearances": ["pii"]}, headers=plain)
        assert unchanged.status_code == 200, unchanged.text
        removed = await http.patch(one, json={"clearances": []}, headers=plain)
        assert removed.status_code == 200, removed.text
        assert removed.json()["clearances"] == []

        missing = await http.patch(f"{HOOKS}/whk_nope", json={"clearances": []}, headers=plain)
        assert missing.status_code == 404

        audit = await http.get(f"{API}/domains/acme/audit", headers=installation_admin.headers)
        assert audit.status_code == 200, audit.text
        updates = [e for e in audit.json()["items"] if e["action"] == "webhook.update"]
        assert len(updates) == 3, audit.text
        assert updates[0]["details"]["clearances_before"] == ["pii"]
        assert updates[0]["details"]["clearances"] == []
