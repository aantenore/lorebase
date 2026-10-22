"""Herald against the in-process Keeper application, with the `supply` Domain in Postgres.

The Domain is loaded through the store commit pipeline by the Keeper test loader; an agent
principal with an API key calls every tool through the SDK's MCP client over the in-memory
JSON-RPC transport. Keeper is reached over `httpx.ASGITransport`, so the path is the real one:
MCP tool -> Herald -> Keeper REST (api_key auth) -> store.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from mcp import Client
from mcp.types import CallToolResult, TextContent
from sqlalchemy.ext.asyncio import async_sessionmaker

from lorebase_herald.keeper import KeeperClients
from lorebase_herald.server import create_server
from lorebase_herald.settings import HeraldSettings
from lorebase_keeper.app import create_app
from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.config import ApiKeySettings
from lorebase_platform.settings import Settings
from lorebase_store import migrations, testing
from lorebase_store.auth import SqlAuthStore
from lorebase_store.db import create_engine
from lorebase_store.models.enums import PrincipalType
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings

pytestmark = pytest.mark.integration

PEPPER = "herald-integration-pepper-0123456789"
BASE_URL = "http://keeper.test"
LOADER = Path(__file__).resolve().parents[3] / "keeper" / "tests" / "integration" / "supply.py"


def _supply_loader() -> ModuleType:
    """Keeper's loader of the `supply` example (schema and CSV rows through the commit
    pipeline), shared instead of duplicated."""
    spec = importlib.util.spec_from_file_location("lorebase_supply_loader", LOADER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


supply = _supply_loader()


@dataclass
class Seeded:
    url: str
    agent_key: str
    revisions: dict[str, str]


async def _seed(url: str) -> Seeded:
    engine = create_engine(DatabaseSettings(), url=url, pooled=False)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            _, (created, schema_commit, data) = await supply.load_supply(
                session, StoreRuntime.from_settings(StoreSettings()), supply.Clock()
            )
        store = SqlAuthStore(sessions)
        codec = ApiKeyCodec(ApiKeySettings(pepper=PEPPER))
        async with store.unit() as unit:
            agent = await unit.principals.add(PrincipalType.SERVICE, "herald agent")
            await unit.grants.add(
                domain_id=supply.DOMAIN, subject=agent.id, role="editor", created_by="usr_seed"
            )
            generated = codec.generate()
            await unit.api_keys.add(
                principal_id=agent.id,
                prefix=generated.prefix,
                key_hash=generated.key_hash,
                scopes=[f"domain:{supply.DOMAIN}"],
            )
    finally:
        await engine.dispose()
    return Seeded(
        url=url,
        agent_key=generated.token,
        revisions={
            "created": created.revision_id.hex(),
            "schema": schema_commit.revision_id.hex(),
            "data": data.revision_id.hex(),
        },
    )


@pytest.fixture(scope="module")
def seeded() -> Iterator[Seeded]:
    with testing.postgres_server() as server, testing.scratch_database(server) as url:
        migrations.upgrade(database=DatabaseSettings(), store=StoreSettings(), url=url)
        yield asyncio.run(_seed(url))


@pytest.fixture
async def keeper_transport(seeded: Seeded) -> AsyncIterator[httpx.AsyncBaseTransport]:
    settings = Settings(
        keeper={"auth": {"mode": "api_key", "api_keys": {"pepper": PEPPER}}},
        database={"dsn": seeded.url},
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield httpx.ASGITransport(app=app, raise_app_exceptions=False)


def _herald(transport: httpx.AsyncBaseTransport, api_key: str) -> Client:
    settings = HeraldSettings.model_validate(
        {"domain": supply.DOMAIN, "keeper": {"base_url": BASE_URL, "api_key": api_key}}
    )
    return Client(
        create_server(settings, KeeperClients(settings.keeper, transport=transport)), mode="legacy"
    )


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert not result.is_error, block.text
    return block.text


async def test_tools_over_mcp_against_keeper(
    seeded: Seeded, keeper_transport: httpx.AsyncBaseTransport
) -> None:
    client = _herald(keeper_transport, seeded.agent_key)
    revs = seeded.revisions
    async with client:
        call = client.call_tool
        listed = await client.list_tools()
        assert len(listed.tools) == 13

        schema = _text(await call("lorebase_schema", {}))
        assert schema.startswith("schema supply@main rev ")
        assert "Supplier [key supplier_code; title legal_name" in schema
        assert "supplied_by: Part -> Supplier (many_to_many; inverse supplies)" in schema

        record = _text(await call("lorebase_get", {"ref": "Supplier/S-1001"}))
        assert record.startswith('Supplier/S-1001 "Kavsen Alloys Sp. z o.o."')
        assert "country: PL" in record
        assert "links: " in record

        rows = _text(
            await call(
                "lorebase_query",
                {
                    "query": {
                        "kind": "Part",
                        "where": {"attr": "category", "op": "eq", "value": "bearing"},
                        "select": ["category"],
                        "limit": 3,
                    }
                },
            )
        )
        assert rows.splitlines()[0].endswith(": 3 rows")
        assert "category=bearing" in rows
        assert "next_cursor: " in rows

        hits = _text(await call("lorebase_search", {"query": "bearing", "kinds": ["Part"]}))
        assert "(lexical)" in hits.splitlines()[0]
        assert "1. Part/" in hits

        hood = _text(
            await call(
                "lorebase_neighborhood",
                {"seeds": ["Part/P-10001"], "relations": ["supplied_by"]},
            )
        )
        assert "Part/P-10001 -supplied_by-> Supplier/S-1005" in hood

        pack = _text(
            await call("lorebase_context", {"records": ["Part/P-10001"], "token_budget": 800})
        )
        # Keeper's own pack (`POST .../context`), within the budget
        assert pack.startswith("# Context: supply/main")
        assert "### Part/P-10001: Tapered roller bearing 6305" in pack
        assert "- Part/P-10001 -> Supplier/S-1005" in pack

        lineage = _text(await call("lorebase_explain", {"ref": "Supplier/S-1001"}))
        assert "legal_name = Kavsen Alloys Sp. z o.o. <- import by usr_loader" in lineage
        assert "LINKS:" in lineage

        history = _text(await call("lorebase_history", {"ref": "Supplier/S-1001"}))
        assert revs["data"][:12] in history
        values = _text(await call("lorebase_lineage", {"ref": "Supplier/S-1001"}))
        assert "legal_name" in values

        changes = _text(await call("lorebase_changes", {"since": revs["schema"]}))
        assert f"{revs['data']} " in changes
        assert "Import the supply example data" in changes
        assert revs["schema"] not in changes.splitlines()[1]

        diff = _text(
            await call(
                "lorebase_diff",
                {
                    "from_revision": revs["schema"],
                    "to_revision": revs["data"],
                    "kinds": ["Supplier"],
                    "limit": 2,
                },
            )
        )
        assert diff.startswith(f"diff {revs['schema']}..{revs['data']}")
        assert "summary: Supplier +" in diff
        assert "+ Supplier/S-" in diff

        dry = _text(
            await call(
                "lorebase_execute",
                {
                    "operation": "update_supplier",
                    "parameters": {"key": "S-1001", "city": "Lodz"},
                },
            )
        )
        assert dry.startswith("dry_run update_supplier on main")
        assert "1 edits, 0 violations" in dry
        # Direct commits to `main` need review: the dry run says so before any write.
        assert dry.endswith("run it on a work branch, then open a Proposal")
        after = _text(await call("lorebase_get", {"ref": "Supplier/S-1001"}))
        assert "city: Poznan" in after  # the dry run wrote nothing

        branches = _text(await call("lorebase_branches", {}))
        assert branches.startswith("main (default")

        resource = await client.read_resource("lorebase://supply/Supplier/S-1001")
        assert "Supplier/S-1001" in resource.contents[0].text  # type: ignore[union-attr]

        missing = await call("lorebase_get", {"ref": "Supplier/S-0000"})
        assert missing.is_error
        assert "[not-found]" in missing.content[0].text  # type: ignore[union-attr]


async def test_calls_without_a_valid_agent_key_are_refused(
    keeper_transport: httpx.AsyncBaseTransport,
) -> None:
    client = _herald(keeper_transport, "lbk_unknownprefix_notavalidsecret")
    async with client:
        result: Any = await client.call_tool("lorebase_schema", {})
    assert result.is_error
    assert "[unauthorized]" in result.content[0].text
