"""Who sees which agent trace steps, through the API, as a non-superuser role."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from lorebase_keeper.app import create_app
from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.config import ApiKeySettings
from lorebase_platform.settings import Settings
from lorebase_store.db import create_engine
from lorebase_store.models.enums import GrantScope, PrincipalType
from lorebase_store.settings import DatabaseSettings
from lorebase_store.tracing import NewTraceStep, TraceRepository

from .test_auth_postgres import owner_store

pytestmark = pytest.mark.integration

PEPPER = "test-pepper-0123456789-0123456789-abcdef"
CODEC = ApiKeyCodec(ApiKeySettings(pepper=PEPPER))
TRACE = "/api/v1/trace/sessions"
REFS = {"records": [["Part", "P-1"], ["Supplier", "S-1"]], "links": []}
QUERY = {"body": {"q": "bearing", "where": {"attr": "country", "op": "eq", "value": "PL"}}}
"""As `capture_filter_values: non_sensitive` stores it: the literal follows the visibility
of the query (withheld from readers not cleared in the Domain)."""


async def seed(database_url: str) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Domains acme and globex; principals and their keys; steps of the agent `bot`."""
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
    grants: dict[str, list[tuple[str, str, GrantScope, str | None]]] = {
        "bot": [
            ("acme", "viewer", GrantScope.DOMAIN, None),
            ("globex", "viewer", GrantScope.DOMAIN, None),
        ],
        "peer": [("acme", "viewer", GrantScope.DOMAIN, None)],
        "admin": [("acme", "admin", GrantScope.DOMAIN, None)],
        "narrow": [
            ("acme", "tracer", GrantScope.DOMAIN, None),
            ("acme", "viewer", GrantScope.KIND, "Part"),
        ],
    }
    headers: dict[str, dict[str, str]] = {}
    ids: dict[str, str] = {}
    async with owner_store(database_url) as store, store.unit() as unit:
        for name, granted in grants.items():
            principal = await unit.principals.add(PrincipalType.SERVICE, name)
            ids[name] = principal.id
            for domain, role, scope, ref in granted:
                await unit.grants.add(
                    domain_id=domain,
                    subject=principal.id,
                    role=role,
                    created_by="usr_seed",
                    scope_type=scope,
                    scope_ref=ref,
                )
            key = CODEC.generate()
            await unit.api_keys.add(
                principal_id=principal.id, prefix=key.prefix, key_hash=key.key_hash, scopes=[]
            )
            headers[name] = {"Authorization": f"Bearer {key.token}"}
    now = datetime.now(UTC)
    engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            bot = ids["bot"]
            await TraceRepository(session).append_many(
                [
                    NewTraceStep(
                        session_id="s1",
                        actor=bot,
                        type="call",
                        at=now,
                        domain_id="acme",
                        query=QUERY,
                        refs=REFS,
                        result={"counts": {"items": 2}, "mode": "lexical"},
                    ),
                    NewTraceStep(
                        session_id="s1",
                        actor=bot,
                        type="note",
                        at=now + timedelta(seconds=1),
                        note="plain note",
                    ),
                    NewTraceStep(
                        session_id="s1",
                        actor=bot,
                        type="call",
                        at=now + timedelta(seconds=2),
                        domain_id="globex",
                    ),
                ],
                max_per_session=100,
            )
    finally:
        await engine.dispose()
    return headers, ids


@asynccontextmanager
async def running(app_database_url: str) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        keeper={
            "auth": {
                "mode": "api_key",
                "api_keys": {"pepper": PEPPER},
                "authz": {"custom_roles": {"tracer": ["trace.read"]}},
            },
        },
        database={"dsn": app_database_url},
    )
    app: FastAPI = create_app(settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://keeper.test") as http,
    ):
        yield http


async def test_principals_see_their_own_steps_and_trace_readers_their_domains(
    database_url: str, app_database_url: str
) -> None:
    headers, ids = await seed(database_url)
    bots = {"actor": ids["bot"]}
    async with running(app_database_url) as http:
        assert (await http.get(TRACE)).status_code == 401

        own = (await http.get(f"{TRACE}/s1", headers=headers["bot"])).json()
        assert [s["type"] for s in own["steps"]] == ["call", "note", "call"]
        assert own["steps"][0]["query"] == QUERY

        assert (
            await http.get(f"{TRACE}/s1", params=bots, headers=headers["peer"])
        ).status_code == 404
        assert (await http.get(TRACE, headers=headers["peer"])).json()["items"] == []

        admin = (await http.get(f"{TRACE}/s1", params=bots, headers=headers["admin"])).json()
        [step] = admin["steps"]  # acme only: not the note, not globex
        assert step["domain_id"] == "acme"
        assert step["query"] == QUERY
        assert len(step["refs"]["records"]) == 2

        narrow = (await http.get(f"{TRACE}/s1", params=bots, headers=headers["narrow"])).json()
        [step] = narrow["steps"]
        assert step["query"] is None
        assert step["result"] == {"counts": {"items": 2}}
        assert step["refs"]["records"] == [{"kind": "Part", "key": "P-1"}]
        assert step["refs"]["truncated"] is True
        assert step["withheld"] is True
        mine = await http.get(f"{TRACE}/s1", headers=headers["admin"])
        assert mine.status_code == 404  # without actor: the caller's own session

        listed = (await http.get(TRACE, headers=headers["admin"])).json()["items"]
        assert [(s["session_id"], s["calls"], s["notes"], s["domains"]) for s in listed] == [
            ("s1", 1, 0, ["acme"])
        ]
        for name in ("peer", "admin"):
            other = await http.post(
                f"{TRACE}/s1/notes",
                json={"text": "x", "domain_id": "globex"},
                headers=headers[name],
            )
            assert other.status_code == 403
