"""Administration endpoints: API keys, principals, grants, audit."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_store.models.enums import PrincipalType

PEPPER = "test-pepper-0123456789-0123456789-abcdef"
API_KEYS: dict[str, Any] = {"mode": "api_key", "api_keys": {"pepper": PEPPER}}
BASE = "/api/v1/domains/acme"

ClientFor = Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]


def headers_for(store: Any, codec: ApiKeyCodec, principal: str, role: str | None) -> dict[str, str]:
    if principal not in store.principal_rows:
        store.add_principal(principal, PrincipalType.SERVICE)
    if role:
        store.add_grant("acme", principal, role)
    token, _ = store.add_key(codec, principal)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin(store: Any, codec: ApiKeyCodec) -> dict[str, str]:
    return headers_for(store, codec, "svc_admin", "admin")


@pytest.fixture
async def http(
    auth_app: Callable[..., FastAPI], client_for: ClientFor
) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(auth_app(API_KEYS)) as client:
        yield client


# API keys -----------------------------------------------------------------------------------


async def test_create_key_returns_the_secret_once_and_it_works(
    http: httpx.AsyncClient, store: Any, admin: dict[str, str], clock: Any
) -> None:
    response = await http.post(
        f"{BASE}/api-keys", json={"name": "ingest bot", "role": "editor"}, headers=admin
    )
    body = response.json()
    assert response.status_code == 201
    assert body["key"].startswith("lbk_")
    assert body["prefix"] in body["key"]
    assert body["role"] == "editor"
    assert body["scopes"] == ["domain:acme"]
    assert body["principal_id"].startswith("svc_")
    assert body["expires_at"].startswith("2027-10-20")  # default 365 days from the test clock

    stored = store.key_rows[body["id"]]
    secret = body["key"].split("_", 2)[2]
    assert stored.hash != secret.encode()
    assert secret not in repr(stored)
    assert store.principal_rows[body["principal_id"]].type is PrincipalType.SERVICE
    assert store.principal_rows[body["principal_id"]].display_name == "ingest bot"
    [grant] = [g for g in store.grant_rows if g.subject == body["principal_id"]]
    assert (grant.domain_id, grant.role, grant.created_by) == ("acme", "editor", "svc_admin")

    # the new key authenticates, with the role it was given
    bot = {"Authorization": f"Bearer {body['key']}"}
    assert (await http.get(f"{BASE}/api-keys", headers=bot)).status_code == 403
    assert (await http.get(f"{BASE}/grants", headers=bot)).status_code == 403
    other_domain = await http.get("/api/v1/domains/globex/grants", headers=bot)
    assert other_domain.status_code == 403


async def test_default_role_is_agent_reader(
    http: httpx.AsyncClient, store: Any, admin: dict[str, str]
) -> None:
    body = (await http.post(f"{BASE}/api-keys", json={"name": "bot"}, headers=admin)).json()
    assert body["role"] == "agent-reader"


async def test_listing_never_shows_secrets_and_includes_revoked_keys(
    http: httpx.AsyncClient, store: Any, admin: dict[str, str]
) -> None:
    created = (await http.post(f"{BASE}/api-keys", json={"name": "bot"}, headers=admin)).json()
    await http.delete(f"{BASE}/api-keys/{created['id']}", headers=admin)
    page = (await http.get(f"{BASE}/api-keys", headers=admin)).json()
    ids = [k["id"] for k in page["items"]]
    assert created["id"] in ids
    listed = next(k for k in page["items"] if k["id"] == created["id"])
    assert listed["revoked_at"] is not None
    assert "key" not in listed
    assert "hash" not in listed
    assert created["key"].split("_", 2)[2] not in str(page)


async def test_key_lifetime_follows_the_request_and_the_limits(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, admin: dict[str, str]
) -> None:
    app = auth_app(
        {**API_KEYS, "api_keys": {"pepper": PEPPER, "default_ttl_days": 30, "max_ttl_days": 90}}
    )
    async with client_for(app) as http:
        default = await http.post(f"{BASE}/api-keys", json={"name": "a"}, headers=admin)
        short = await http.post(
            f"{BASE}/api-keys", json={"name": "b", "ttl_days": 7}, headers=admin
        )
        long = await http.post(
            f"{BASE}/api-keys", json={"name": "c", "ttl_days": 91}, headers=admin
        )
    assert default.json()["expires_at"].startswith("2026-11-19")
    assert short.json()["expires_at"].startswith("2026-10-27")
    assert long.status_code == 422
    assert long.json()["errors"][0]["pointer"] == "/body/ttl_days"


async def test_keys_can_be_created_without_expiry_when_configured(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, admin: dict[str, str]
) -> None:
    app = auth_app({**API_KEYS, "api_keys": {"pepper": PEPPER, "default_ttl_days": None}})
    async with client_for(app) as http:
        body = (await http.post(f"{BASE}/api-keys", json={"name": "a"}, headers=admin)).json()
    assert body["expires_at"] is None


async def test_unknown_role_is_a_validation_problem(
    http: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    response = await http.post(
        f"{BASE}/api-keys", json={"name": "bot", "role": "emperor"}, headers=admin
    )
    assert response.status_code == 422
    assert response.json()["errors"][0]["pointer"] == "/body/role"


async def test_additional_key_for_an_existing_service_principal(
    http: httpx.AsyncClient, store: Any, admin: dict[str, str]
) -> None:
    first = (await http.post(f"{BASE}/api-keys", json={"name": "bot"}, headers=admin)).json()
    grants_before = len(store.grant_rows)
    second = await http.post(
        f"{BASE}/api-keys",
        json={"name": "ignored", "principal_id": first["principal_id"]},
        headers=admin,
    )
    assert second.status_code == 201
    assert second.json()["principal_id"] == first["principal_id"]
    assert second.json()["role"] is None
    assert len(store.grant_rows) == grants_before


async def test_key_for_a_user_or_unknown_principal_is_refused(
    http: httpx.AsyncClient, store: Any, admin: dict[str, str]
) -> None:
    store.add_principal("usr_person", PrincipalType.USER)
    for principal in ("usr_person", "svc_missing"):
        response = await http.post(
            f"{BASE}/api-keys", json={"name": "x", "principal_id": principal}, headers=admin
        )
        assert response.status_code == 404


async def test_only_admins_manage_keys(
    http: httpx.AsyncClient, store: Any, codec: ApiKeyCodec
) -> None:
    for role in ("viewer", "editor", "modeler", "maintainer", None):
        who = headers_for(store, codec, f"svc_{role}", role)
        create = await http.post(f"{BASE}/api-keys", json={"name": "x"}, headers=who)
        listing = await http.get(f"{BASE}/api-keys", headers=who)
        revoke = await http.delete(f"{BASE}/api-keys/key_1", headers=who)
        assert (create.status_code, listing.status_code, revoke.status_code) == (403,) * 3, role
        assert create.json()["permission"] == "apikey.manage"
    assert not [k for k in store.key_rows.values() if k.scopes]  # nothing was created


async def test_an_admin_of_another_domain_cannot_manage_these_keys(
    http: httpx.AsyncClient, store: Any, codec: ApiKeyCodec
) -> None:
    store.add_principal("svc_other", PrincipalType.SERVICE)
    store.add_grant("globex", "svc_other", "admin")
    token, _ = store.add_key(codec, "svc_other")
    response = await http.get(f"{BASE}/api-keys", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


async def test_revoked_key_stops_authenticating(
    http: httpx.AsyncClient, store: Any, admin: dict[str, str]
) -> None:
    created = (
        await http.post(f"{BASE}/api-keys", json={"name": "bot", "role": "admin"}, headers=admin)
    ).json()
    bot = {"Authorization": f"Bearer {created['key']}"}
    assert (await http.get(f"{BASE}/grants", headers=bot)).status_code == 200
    assert (await http.delete(f"{BASE}/api-keys/{created['id']}", headers=admin)).status_code == 204
    assert (await http.get(f"{BASE}/grants", headers=bot)).status_code == 401
    # revoking again is not an error
    assert (await http.delete(f"{BASE}/api-keys/{created['id']}", headers=admin)).status_code == 204


async def test_revoking_a_key_of_another_domain_is_not_found(
    http: httpx.AsyncClient, store: Any, codec: ApiKeyCodec, admin: dict[str, str]
) -> None:
    store.add_principal("svc_x", PrincipalType.SERVICE)
    _, foreign = store.add_key(codec, "svc_x", scopes=["domain:globex"])
    response = await http.delete(f"{BASE}/api-keys/{foreign.id}", headers=admin)
    assert response.status_code == 404
    assert response.json()["code"] == "not-found"
    assert store.key_rows[foreign.id].revoked_at is None


async def test_key_listing_is_paginated(http: httpx.AsyncClient, admin: dict[str, str]) -> None:
    for n in range(4):
        await http.post(f"{BASE}/api-keys", json={"name": f"bot {n}"}, headers=admin)
    first = (await http.get(f"{BASE}/api-keys", params={"limit": 3}, headers=admin)).json()
    second = (
        await http.get(
            f"{BASE}/api-keys", params={"limit": 3, "cursor": first["next_cursor"]}, headers=admin
        )
    ).json()
    assert len(first["items"]) == 3
    assert first["next_cursor"]
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None
    ids = [k["id"] for k in first["items"] + second["items"]]
    assert len(set(ids)) == 4


async def test_keys_need_api_key_mode(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, store: Any
) -> None:
    store.add_principal("usr_a", PrincipalType.USER, subject="alice-subject")
    store.add_grant("acme", "usr_a", "admin")
    app = auth_app({"mode": "none"})
    async with client_for(app) as http:
        response = await http.post(f"{BASE}/api-keys", json={"name": "x"})
    assert response.status_code == 400
    assert "keeper.auth.mode" in response.json()["detail"]


# principals ---------------------------------------------------------------------------------


async def test_principals_are_listed_for_admins(
    http: httpx.AsyncClient, store: Any, admin: dict[str, str]
) -> None:
    store.add_principal("usr_ada", PrincipalType.USER, subject="ada")
    page = (await http.get("/api/v1/principals", headers=admin)).json()
    assert {p["id"] for p in page["items"]} >= {"svc_admin", "usr_ada"}
    ada = next(p for p in page["items"] if p["id"] == "usr_ada")
    assert ada["external_subject"] == "ada"
    only_users = (
        await http.get("/api/v1/principals", params={"type": "user"}, headers=admin)
    ).json()
    assert [p["id"] for p in only_users["items"]] == ["usr_ada"]


async def test_principals_are_not_listed_for_non_admins(
    http: httpx.AsyncClient, store: Any, codec: ApiKeyCodec
) -> None:
    who = headers_for(store, codec, "svc_viewer", "viewer")
    response = await http.get("/api/v1/principals", headers=who)
    assert response.status_code == 403
    assert response.json()["permission"] == "principal.read"


async def test_principals_listing_is_paginated(
    http: httpx.AsyncClient, store: Any, admin: dict[str, str]
) -> None:
    for n in range(5):
        store.add_principal(f"usr_{n}", PrincipalType.USER, subject=f"s{n}")
    first = (await http.get("/api/v1/principals", params={"limit": 4}, headers=admin)).json()
    second = (
        await http.get(
            "/api/v1/principals", params={"limit": 4, "cursor": first["next_cursor"]}, headers=admin
        )
    ).json()
    assert len(first["items"]) == 4
    assert len(second["items"]) == 2
    assert second["next_cursor"] is None


# grants -------------------------------------------------------------------------------------


async def test_grant_lifecycle_takes_effect_immediately(
    http: httpx.AsyncClient, store: Any, admin: dict[str, str], codec: ApiKeyCodec
) -> None:
    reader = headers_for(store, codec, "svc_reader", None)
    probe = f"{BASE}/grants"
    assert (await http.get(probe, headers=reader)).status_code == 403

    created = await http.post(probe, json={"subject": "svc_reader", "role": "admin"}, headers=admin)
    body = created.json()
    assert created.status_code == 201
    assert (body["domain_id"], body["subject"], body["role"]) == ("acme", "svc_reader", "admin")
    assert body["scope_type"] == "domain"
    assert body["created_by"] == "svc_admin"
    assert (await http.get(probe, headers=reader)).status_code == 200

    listing = (await http.get(probe, headers=admin)).json()
    assert body["id"] in [g["id"] for g in listing["items"]]

    assert (await http.delete(f"{probe}/{body['id']}", headers=admin)).status_code == 204
    assert (await http.get(probe, headers=reader)).status_code == 403
    assert (await http.delete(f"{probe}/{body['id']}", headers=admin)).status_code == 404


async def test_scoped_grants(http: httpx.AsyncClient, store: Any, admin: dict[str, str]) -> None:
    store.add_principal("svc_bot", PrincipalType.SERVICE)
    created = await http.post(
        f"{BASE}/grants",
        json={
            "subject": "svc_bot",
            "role": "editor",
            "scope_type": "branch",
            "scope_ref": "agent/*",
        },
        headers=admin,
    )
    assert created.status_code == 201
    assert (created.json()["scope_type"], created.json()["scope_ref"]) == ("branch", "agent/*")


@pytest.mark.parametrize(
    "body",
    [
        {"subject": "svc_bot", "role": "editor", "scope_type": "kind"},
        {"subject": "svc_bot", "role": "editor", "scope_ref": "Supplier"},
        {"subject": "svc_bot", "role": "nonsense"},
        {"subject": "svc_bot", "role": "editor", "scope_type": "galaxy"},
        {"subject": "", "role": "editor"},
    ],
)
async def test_invalid_grants_are_validation_problems(
    http: httpx.AsyncClient, store: Any, admin: dict[str, str], body: dict[str, str]
) -> None:
    store.add_principal("svc_bot", PrincipalType.SERVICE)
    response = await http.post(f"{BASE}/grants", json=body, headers=admin)
    assert response.status_code == 422
    assert response.json()["code"] == "validation-failed"


async def test_granting_to_an_unknown_subject_is_not_found(
    http: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    response = await http.post(
        f"{BASE}/grants", json={"subject": "usr_ghost", "role": "viewer"}, headers=admin
    )
    assert response.status_code == 404


async def test_groups_can_be_granted_roles(
    http: httpx.AsyncClient, store: Any, admin: dict[str, str]
) -> None:
    store.group_rows["grp_data"] = "idp-data"
    response = await http.post(
        f"{BASE}/grants", json={"subject": "grp_data", "role": "viewer"}, headers=admin
    )
    assert response.status_code == 201


async def test_only_admins_manage_grants(
    http: httpx.AsyncClient, store: Any, codec: ApiKeyCodec
) -> None:
    store.add_principal("svc_target", PrincipalType.SERVICE)
    for role in ("viewer", "maintainer", "modeler"):
        who = headers_for(store, codec, f"svc_{role}", role)
        create = await http.post(
            f"{BASE}/grants", json={"subject": "svc_target", "role": "admin"}, headers=who
        )
        assert create.status_code == 403
        assert create.json()["permission"] == "grant.manage"
    assert all(g.subject != "svc_target" for g in store.grant_rows)


async def test_grants_in_another_domain_are_invisible_to_this_domains_admin(
    http: httpx.AsyncClient, store: Any, admin: dict[str, str]
) -> None:
    other = store.add_grant("globex", "svc_x", "viewer")
    page = (await http.get(f"{BASE}/grants", headers=admin)).json()
    assert other.id not in [g["id"] for g in page["items"]]
    assert (await http.delete(f"{BASE}/grants/{other.id}", headers=admin)).status_code == 404


async def test_granting_in_an_unknown_domain_is_not_found(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, store: Any
) -> None:
    store.add_principal("svc_bot", PrincipalType.SERVICE)
    async with client_for(auth_app({"mode": "none"})) as http:
        response = await http.post(
            "/api/v1/domains/ghost/grants", json={"subject": "svc_bot", "role": "viewer"}
        )
    assert response.status_code == 404
    assert response.json()["detail"] == "Domain not found."


# audit --------------------------------------------------------------------------------------


async def test_admins_read_the_audit_log_of_their_domain(
    http: httpx.AsyncClient, store: Any, admin: dict[str, str]
) -> None:
    await http.post(f"{BASE}/api-keys", json={"name": "bot"}, headers=admin)
    await http.post(
        f"{BASE}/grants", json={"subject": "svc_admin", "role": "viewer"}, headers=admin
    )
    page = (await http.get(f"{BASE}/audit", headers=admin)).json()
    assert [e["action"] for e in page["items"]] == ["grant.create", "apikey.create"]
    entry = page["items"][0]
    assert entry["actor"] == "svc_admin"
    assert entry["domain_id"] == "acme"
    assert entry["details"]["outcome"] == "success"
    assert len(entry["entry_hash"]) == 64

    first = (await http.get(f"{BASE}/audit", params={"limit": 1}, headers=admin)).json()
    second = (
        await http.get(
            f"{BASE}/audit", params={"limit": 1, "cursor": first["next_cursor"]}, headers=admin
        )
    ).json()
    assert [e["action"] for e in first["items"] + second["items"]] == [
        "grant.create",
        "apikey.create",
    ]


async def test_audit_is_for_admins_only(
    http: httpx.AsyncClient, store: Any, codec: ApiKeyCodec
) -> None:
    who = headers_for(store, codec, "svc_maint", "maintainer")
    response = await http.get(f"{BASE}/audit", headers=who)
    assert response.status_code == 403
    assert response.json()["permission"] == "audit.read"


async def test_group_and_key_changes_are_audited_with_their_targets(
    http: httpx.AsyncClient, store: Any, admin: dict[str, str]
) -> None:
    key = (await http.post(f"{BASE}/api-keys", json={"name": "bot"}, headers=admin)).json()
    grant = (
        await http.post(
            f"{BASE}/grants", json={"subject": key["principal_id"], "role": "viewer"}, headers=admin
        )
    ).json()
    await http.delete(f"{BASE}/grants/{grant['id']}", headers=admin)
    await http.delete(f"{BASE}/api-keys/{key['id']}", headers=admin)
    recorded = [(e.action, e.target) for e in store.audit_rows]
    assert recorded == [
        ("apikey.create", key["id"]),
        ("grant.create", grant["id"]),
        ("grant.revoke", grant["id"]),
        ("apikey.revoke", key["id"]),
    ]


# envelope -----------------------------------------------------------------------------------


async def test_errors_use_the_problem_details_envelope(
    http: httpx.AsyncClient, store: Any, admin: dict[str, str]
) -> None:
    responses = [
        await http.get(f"{BASE}/grants"),  # 401
        await http.delete(f"{BASE}/grants/grt_missing", headers=admin),  # 404
        await http.post(f"{BASE}/api-keys", json={}, headers=admin),  # 422
    ]
    for response in responses:
        body = response.json()
        assert response.headers["content-type"] == "application/problem+json"
        assert body["type"] == f"urn:lorebase:problem:{body['code']}"
        assert body["request_id"] == response.headers["x-request-id"]
        assert body["instance"].startswith("/api/v1/")
    assert [r.status_code for r in responses] == [401, 404, 422]


async def test_administration_without_a_database_is_unavailable(
    make_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    async with client_for(make_app({"auth": {"mode": "none"}})) as http:
        response = await http.get(f"{BASE}/grants")
    assert response.status_code == 503
    assert response.json()["code"] == "unavailable"


def test_default_ttl_in_the_test_clock_is_a_year(clock: Any) -> None:
    assert (clock.now + timedelta(days=365)).year == 2027
