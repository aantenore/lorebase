"""Webhooks are created only when their signing keys are sealed, unless explicitly allowed."""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.integration

API = "/api/v1"
HOOKS = f"{API}/domains/acme/webhooks"
KEY = "webhook-key-0123456789-0123456789-abcdef"


@pytest.mark.parametrize(
    ("webhooks", "status"),
    [
        ({}, 409),
        ({"encryption_key": KEY}, 201),
        ({"allow_unsealed_keys": True}, 201),
    ],
    ids=["no-key", "sealed", "opt-out"],
)
async def test_creation_needs_an_encryption_key(
    keeper: Any, installation_admin: Any, webhooks: dict[str, Any], status: int
) -> None:
    async with keeper({"webhooks": webhooks}) as http:
        created = await http.post(
            f"{API}/domains", json={"id": "acme"}, headers=installation_admin.headers
        )
        assert created.status_code == 201, created.text
        hook = await http.post(
            HOOKS, json={"url": "https://hooks.example.com/in"}, headers=installation_admin.headers
        )
        assert hook.status_code == status, hook.text
        listed = await http.get(HOOKS, headers=installation_admin.headers)
    if status == 409:
        assert hook.json()["code"] == "encryption_key_required"
        assert hook.json()["setting"] == "webhooks.encryption_key"
        assert listed.json()["items"] == []
    else:
        assert hook.json()["secret"]
        assert len(listed.json()["items"]) == 1
