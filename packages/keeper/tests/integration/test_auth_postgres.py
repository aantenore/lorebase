"""Authentication, authorization and audit against a real Postgres, as a non-superuser role."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from lorebase_keeper.app import create_app
from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.config import ApiKeySettings
from lorebase_platform.settings import Settings
from lorebase_store.auth import SqlAuthStore
from lorebase_store.db import create_engine
from lorebase_store.models.enums import PrincipalType
from lorebase_store.settings import DatabaseSettings

pytestmark = pytest.mark.integration

PEPPER = "test-pepper-0123456789-0123456789-abcdef"
CODEC = ApiKeyCodec(ApiKeySettings(pepper=PEPPER))
BASE = "/api/v1/domains/acme"


@asynccontextmanager
async def owner_store(database_url: str) -> AsyncIterator[SqlAuthStore]:
    """The auth store as the database owner, for seeding and inspecting."""
    engine: AsyncEngine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    try:
        yield SqlAuthStore(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


async def seed(database_url: str) -> str:
    """Domains `acme` and `globex`, and an admin of acme with a key; returns the key."""
    engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    try:
        async with engine.begin() as connection:
            for domain in ("acme", "globex"):
                await connection.execute(
                    text(
                        "INSERT INTO domains (id, display_name, leaf_size, created_at, created_by) "
                        "VALUES (:id, :id, 512, :at, 'usr_seed')"
                    ),
                    {"id": domain, "at": datetime(2026, 10, 20, tzinfo=UTC)},
                )
    finally:
        await engine.dispose()
    async with owner_store(database_url) as store, store.unit() as unit:
        admin = await unit.principals.add(PrincipalType.SERVICE, "bootstrap admin")
        await unit.grants.add(
            domain_id="acme", subject=admin.id, role="admin", created_by="usr_seed"
        )
        generated = CODEC.generate()
        await unit.api_keys.add(
            principal_id=admin.id,
            prefix=generated.prefix,
            key_hash=generated.key_hash,
            scopes=["domain:acme"],
        )
    return generated.token


@asynccontextmanager
async def running(app_database_url: str, **auth: object) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        keeper={"auth": {"mode": "api_key", "api_keys": {"pepper": PEPPER}, **auth}},
        database={"dsn": app_database_url},
    )
    app: FastAPI = create_app(settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://keeper.test") as http,
    ):
        yield http


async def test_keys_grants_and_audit_work_end_to_end_under_row_level_security(
    database_url: str, app_database_url: str
) -> None:
    admin_key = await seed(database_url)
    admin = {"Authorization": f"Bearer {admin_key}"}

    async with running(app_database_url) as http:
        assert (await http.get(f"{BASE}/grants")).status_code == 401

        created = await http.post(
            f"{BASE}/api-keys", json={"name": "reader bot", "role": "viewer"}, headers=admin
        )
        assert created.status_code == 201
        bot = {"Authorization": f"Bearer {created.json()['key']}"}

        # the bot reads nothing administrative, in acme or elsewhere
        assert (await http.get(f"{BASE}/grants", headers=bot)).status_code == 403
        assert (await http.get("/api/v1/domains/globex/grants", headers=admin)).status_code == 403

        listed = (await http.get(f"{BASE}/api-keys", headers=admin)).json()
        assert len(listed["items"]) == 2  # the bootstrap admin's key and the new one
        assert created.json()["id"] in [k["id"] for k in listed["items"]]

        granted = await http.post(
            f"{BASE}/grants",
            json={"subject": created.json()["principal_id"], "role": "admin"},
            headers=admin,
        )
        assert granted.status_code == 201
        assert (await http.get(f"{BASE}/grants", headers=bot)).status_code == 200

        grant_id = granted.json()["id"]
        assert (await http.delete(f"{BASE}/grants/{grant_id}", headers=admin)).status_code == 204
        assert (await http.get(f"{BASE}/grants", headers=bot)).status_code == 403

        assert (
            await http.delete(f"{BASE}/api-keys/{created.json()['id']}", headers=admin)
        ).status_code == 204
        assert (await http.get(f"{BASE}/grants", headers=bot)).status_code == 401

        audit = (await http.get(f"{BASE}/audit", headers=admin)).json()
        actions = [e["action"] for e in reversed(audit["items"])]
        denied = "GET /api/v1/domains/{domain_id}/grants"
        assert actions == [
            denied,  # no credential
            "apikey.create",
            denied,  # the bot, before it was granted anything
            "grant.create",
            "grant.revoke",
            denied,  # the bot, after the grant was revoked
            "apikey.revoke",
            denied,  # the bot, after its key was revoked
        ]
        other = (await http.get("/api/v1/domains/globex/audit", headers=admin)).status_code
        assert other == 403

    async with owner_store(database_url) as store, store.unit() as unit:
        verification = await unit.audit.verify()
    assert verification.ok
    assert verification.checked == 10  # eight in acme, the globex denial, the globex audit read


async def test_key_networks_are_stored_and_enforced(
    database_url: str, app_database_url: str
) -> None:
    admin = {"Authorization": f"Bearer {await seed(database_url)}"}
    async with running(app_database_url) as http:  # requests come from 127.0.0.1
        local = await http.post(
            f"{BASE}/api-keys",
            json={"name": "local bot", "allowed_networks": ["127.0.0.1/8", "::1"]},
            headers=admin,
        )
        remote = await http.post(
            f"{BASE}/api-keys",
            json={"name": "remote bot", "allowed_networks": ["203.0.113.0/24"]},
            headers=admin,
        )
        assert local.json()["allowed_networks"] == ["127.0.0.0/8", "::1/128"]
        listed = (await http.get(f"{BASE}/api-keys", headers=admin)).json()["items"]
        networks = {k["id"]: k["allowed_networks"] for k in listed}
        assert networks[remote.json()["id"]] == ["203.0.113.0/24"]
        whoami = "/api/v1/auth/whoami"
        for created, status in ((local, 200), (remote, 401)):
            bearer = {"Authorization": f"Bearer {created.json()['key']}"}
            assert (await http.get(whoami, headers=bearer)).status_code == status
