"""Browser sign-in and sign-out against a real Postgres, as a role subject to row-level
security: the sessions table works through the application role's ordinary row rights."""

from __future__ import annotations

import hashlib

import httpx
import pytest
from fake_oidc import (
    WHOAMI,
    OidcServer,
    csrf_headers,
    keeper_client,
    session_auth,
    session_cookie,
    with_session,
)
from sqlalchemy import text

from lorebase_keeper.app import create_app
from lorebase_platform.settings import Settings
from lorebase_store.db import create_engine
from lorebase_store.settings import DatabaseSettings

pytestmark = pytest.mark.integration


async def stored_sessions(database_url: str) -> list[bytes]:
    engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(text("SELECT id_hash FROM web_sessions"))
            return [bytes(row[0]) for row in rows]
    finally:
        await engine.dispose()


async def test_sign_in_and_out_as_the_application_role(
    app_database_url: str, database_url: str
) -> None:
    server = OidcServer()
    settings = Settings(keeper={"auth": session_auth()}, database={"dsn": app_database_url})
    app = create_app(settings, http_client=server.http)
    async with app.router.lifespan_context(app), keeper_client(app) as http:
        session_id = await session_cookie(http, server)
        who = await http.get(WHOAMI, headers=with_session(session_id))
        assert who.status_code == 200
        assert who.json()["method"] == "session"
        assert await stored_sessions(database_url) == [hashlib.sha256(session_id.encode()).digest()]
        logout = await http.post(
            "/api/v1/auth/logout", headers=await csrf_headers(http, session_id)
        )
        assert logout.status_code == 200
        assert await stored_sessions(database_url) == []
        after: httpx.Response = await http.get(WHOAMI, headers=with_session(session_id))
        assert after.status_code == 401
