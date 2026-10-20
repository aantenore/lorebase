"""A running Keeper with API key authentication, for the write endpoint integration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from lorebase_keeper.app import create_app
from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.config import ApiKeySettings
from lorebase_platform.settings import Settings
from lorebase_store.auth import SqlAuthStore
from lorebase_store.db import create_engine
from lorebase_store.models.enums import PrincipalType
from lorebase_store.settings import DatabaseSettings

PEPPER = "test-pepper-0123456789-0123456789-abcdef"
API = "/api/v1"

Headers = dict[str, str]
KeeperFactory = Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]
RoleKeyFactory = Callable[[httpx.AsyncClient, str, str], Awaitable[Headers]]


@dataclass(frozen=True)
class InstallationAdmin:
    """A service principal listed in `installation_admins`, with an unscoped API key."""

    id: str
    headers: Headers


@pytest.fixture
async def installation_admin(database_url: str) -> InstallationAdmin:
    engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    codec = ApiKeyCodec(ApiKeySettings(pepper=PEPPER))
    try:
        store = SqlAuthStore(async_sessionmaker(engine, expire_on_commit=False))
        async with store.unit() as unit:
            admin = await unit.principals.add(PrincipalType.SERVICE, "installation admin")
            generated = codec.generate()
            await unit.api_keys.add(
                principal_id=admin.id, prefix=generated.prefix, key_hash=generated.key_hash
            )
    finally:
        await engine.dispose()
    return InstallationAdmin(admin.id, {"Authorization": f"Bearer {generated.token}"})


@pytest.fixture
def keeper(app_database_url: str, installation_admin: InstallationAdmin) -> KeeperFactory:
    """Open a client bound to a Keeper that runs as the application role (RLS applies)."""

    @asynccontextmanager
    async def running(
        sections: dict[str, Any] | None = None, **keeper_settings: Any
    ) -> AsyncIterator[httpx.AsyncClient]:
        """`keeper_settings` are `keeper` sub-sections; `sections` other top-level sections."""
        settings = Settings(
            keeper={
                "auth": {
                    "mode": "api_key",
                    "api_keys": {"pepper": PEPPER},
                    "installation_admins": [installation_admin.id],
                    "authz": {"grants_cache_ttl_s": 0},
                },
                **keeper_settings,
            },
            database={"dsn": app_database_url},
            **(sections or {}),
        )
        app = create_app(settings)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://keeper.test") as http,
        ):
            yield http

    return running


@pytest.fixture
def role_key(installation_admin: InstallationAdmin) -> RoleKeyFactory:
    """Create a service principal holding a role in a Domain; returns its headers."""

    async def create(http: httpx.AsyncClient, domain: str, role: str) -> Headers:
        created = await http.post(
            f"{API}/domains/{domain}/api-keys",
            json={"name": f"{role} bot", "role": role},
            headers=installation_admin.headers,
        )
        assert created.status_code == 201, created.text
        return {"Authorization": f"Bearer {created.json()['key']}"}

    return create
