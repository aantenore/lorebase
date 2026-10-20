"""Schema inference needs `schema.write` in the Domain and is audited."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
from fastapi import FastAPI

from lorebase_store.models.enums import PrincipalType

PEPPER = "test-pepper-0123456789-0123456789-abcdef"
API_KEYS: dict[str, Any] = {"mode": "api_key", "api_keys": {"pepper": PEPPER}}
URL = "/api/v1/domains/acme/schema:infer"
FILES = [("files", ("supplier.csv", b"supplier_code,name\nS-1,A\nS-2,B\n", "text/csv"))]

ClientFor = Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]


def _headers(store: Any, codec: Any, role: str) -> dict[str, str]:
    principal = f"svc_{role}"
    store.add_principal(principal, PrincipalType.SERVICE)
    store.add_grant("acme", principal, role)
    token, _ = store.add_key(codec, principal)
    return {"Authorization": f"Bearer {token}"}


async def test_viewers_are_refused(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, store: Any, codec: Any
) -> None:
    async with client_for(auth_app(API_KEYS)) as http:
        response = await http.post(URL, files=FILES, headers=_headers(store, codec, "viewer"))
    assert response.status_code == 403
    assert response.json()["permission"] == "schema.write"


async def test_modelers_get_a_proposal_and_an_audit_entry(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, store: Any, codec: Any
) -> None:
    async with client_for(auth_app(API_KEYS)) as http:
        response = await http.post(URL, files=FILES, headers=_headers(store, codec, "modeler"))
    assert response.status_code == 200, response.text
    [entry] = store.audit_rows
    assert entry.action == "schema.infer"
    assert entry.actor == "svc_modeler"
    assert entry.domain_id == "acme"
    assert entry.details["permission"] == "schema.write"
    assert entry.details["source"] == "csv"
    assert entry.details["kinds"] == 1
