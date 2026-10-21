"""`POST /domains/{d}/schema:infer` with small CSV files and a Postgres source reference."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from lorebase_core.infer import TableProfile
from lorebase_core.model import load_schema
from lorebase_keeper.services import schema_inference
from lorebase_store.profiling import profile_file
from lorebase_store.settings import CsvProfilingSettings

URL = "/api/v1/domains/supply/schema:infer"
SUPPLY_DATA = Path(__file__).resolve().parents[3] / "examples" / "supply" / "data"
SUPPLIER = b"supplier_code,legal_name,country\nS-1,Kavsen,PL\nS-2,Sundello,PL\nS-3,Velkrix,CZ\n"
ORDERS = b"po_number,supplier_id,qty\nPO-1,S-1,5\nPO-2,S-1,7\nPO-3,S-3,1\n"
SOURCE = "secret://env/TEST_ERP_DSN"

ClientFor = Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]
Files = list[tuple[str, tuple[str, bytes, str]]]


def files(*items: tuple[str, bytes]) -> Files:
    return [("files", (name, data, "text/csv")) for name, data in items]


def small() -> Files:
    return files(("supplier.csv", SUPPLIER), ("purchase_orders.csv", ORDERS))


async def test_csv_files_give_a_proposal(client: httpx.AsyncClient) -> None:
    response = await client.post(URL, files=small())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "proposed"
    assert body["domain"] == "supply"
    kinds = {k["api_name"] for k in body["schema"]["kinds"].values()}
    assert kinds == {"PurchaseOrder", "Supplier"}
    relations = list(body["schema"]["relations"].values())
    assert [(r["api_name"], r["cardinality"]) for r in relations] == [("supplier", "many_to_one")]
    assert load_schema(body["yaml"]).kind_by_api_name("Supplier") is not None
    assert {e["element"] for e in body["evidence"]} >= {"kinds.Supplier", "relations.supplier"}
    assert body["issue_count"] == len(body["issues"])
    assert {t["table"] for t in body["tables"]} == {"supplier", "purchase_orders"}


async def test_supply_files(client: httpx.AsyncClient) -> None:
    uploads = files(*((p.name, p.read_bytes()) for p in sorted(SUPPLY_DATA.glob("*.csv"))))
    response = await client.post(URL, files=uploads)
    assert response.status_code == 200, response.text
    schema = response.json()["schema"]
    assert len(schema["kinds"]) == 6
    assert len(schema["relations"]) == 7


@pytest.mark.parametrize(
    ("query", "headers"), [({"format": "yaml"}, {}), ({}, {"Accept": "text/yaml"})]
)
async def test_yaml_answer(
    client: httpx.AsyncClient, query: dict[str, str], headers: dict[str, str]
) -> None:
    response = await client.post(URL, files=small(), params=query, headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/yaml")
    assert load_schema(response.text).kind_by_api_name("PurchaseOrder") is not None


async def test_options_can_be_overridden(client: httpx.AsyncClient) -> None:
    overrides = json.dumps({"singular_kind_names": False})
    response = await client.post(URL, files=small(), data={"options": overrides})
    kinds = {k["api_name"] for k in response.json()["schema"]["kinds"].values()}
    assert "PurchaseOrders" in kinds
    bad = await client.post(URL, files=small(), data={"options": '{"min_inclusion_ratio": 2}'})
    assert bad.status_code == 422
    assert bad.json()["errors"][0]["pointer"] == "/body/options/min_inclusion_ratio"
    garbage = await client.post(URL, files=small(), data={"options": "[1]"})
    assert garbage.status_code == 422


@pytest.mark.parametrize(
    ("uploads", "status", "pointer"),
    [
        (files(), 422, "/body/files"),
        (files(("a.csv", SUPPLIER), ("dir/a.csv", ORDERS)), 422, "/body/files/1"),
        (files(("a.xlsx", SUPPLIER)), 422, "/body/files/0"),
        (files(("a.csv", b"")), 422, "/body/files/0"),
        (files(("a.csv", b"x\n" + b"1\n" * 600)), 413, None),
        (files(*((f"t{i}.csv", SUPPLIER) for i in range(4))), 422, "/body/files"),
    ],
)
async def test_upload_limits(
    make_app: Callable[..., FastAPI],
    client_for: ClientFor,
    uploads: Files,
    status: int,
    pointer: str | None,
) -> None:
    app = make_app({"schema_inference": {"max_files": 3, "max_file_bytes": 1000}})
    async with client_for(app) as http:
        response = await http.post(URL, files=uploads or {"other": ("x", b"")})
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    if pointer is not None:
        assert response.json()["errors"][0]["pointer"] == pointer


async def test_invalid_domain_id(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/domains/Bad%20Id/schema:infer", files=small())
    assert response.status_code == 422


# Postgres sources --------------------------------------------------------------------------


def _source_app(make_app: Callable[..., FastAPI]) -> FastAPI:
    return make_app({"schema_inference": {"allowed_sources": [SOURCE]}})


@pytest.mark.parametrize(
    ("body", "pointer"),
    [
        ({"postgres": {"dsn": "postgresql://u:p@db/erp"}}, "/body/postgres/dsn"),
        ({"postgres": {"dsn": "secret://env/OTHER"}}, "/body/postgres/dsn"),
        ({"postgres": {"dsn": SOURCE}}, "/body/postgres/dsn"),  # not set in the environment
        ({"postgres": {}}, "/body/postgres/dsn"),
        ({"postgres": {"dsn": SOURCE}, "options": {"nope": 1}}, "/body/options/nope"),
    ],
)
async def test_invalid_sources(
    make_app: Callable[..., FastAPI], client_for: ClientFor, body: Any, pointer: str
) -> None:
    async with client_for(_source_app(make_app)) as http:
        response = await http.post(URL, json=body)
    assert response.status_code == 422, response.text
    assert response.json()["errors"][0]["pointer"] == pointer
    assert "postgresql://" not in response.text


async def test_postgres_inference_is_disabled_by_default(client: httpx.AsyncClient) -> None:
    response = await client.post(URL, json={"postgres": {"dsn": SOURCE}})
    assert response.status_code == 422


async def test_unreachable_source_does_not_leak_the_url(
    make_app: Callable[..., FastAPI], client_for: ClientFor, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_url = "postgresql://reader:hunter2-long@/erp?host=/nonexistent-socket-dir"
    monkeypatch.setenv("TEST_ERP_DSN", secret_url)
    async with client_for(_source_app(make_app)) as http:
        response = await http.post(URL, json={"postgres": {"dsn": SOURCE}})
    assert response.status_code == 503, response.text
    assert "hunter2" not in response.text


async def test_postgres_source_gives_a_proposal(
    make_app: Callable[..., FastAPI], client_for: ClientFor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_ERP_DSN", "postgresql://reader@db.example.test/erp")
    seen: dict[str, Any] = {}

    async def fake_profile(
        engine: object, settings: object, *, schema: str, tables: Any
    ) -> list[TableProfile]:
        seen.update(schema=schema, tables=tables)
        csv = CsvProfilingSettings()
        return [profile_file(SUPPLY_DATA / f"{name}.csv", csv) for name in ("supplier", "contract")]

    monkeypatch.setattr(schema_inference, "profile_postgres", fake_profile)
    async with client_for(_source_app(make_app)) as http:
        response = await http.post(
            URL, json={"postgres": {"dsn": SOURCE, "schema": "erp", "tables": ["supplier"]}}
        )
    assert response.status_code == 200, response.text
    assert seen == {"schema": "erp", "tables": ("supplier",)}
    kinds = {k["api_name"] for k in response.json()["schema"]["kinds"].values()}
    assert kinds == {"Contract", "Supplier"}


# Per-Domain sources ------------------------------------------------------------------------

OTHER_SOURCE = "secret://env/TEST_OTHER_DSN"
OTHER_URL = "/api/v1/domains/other/schema:infer"


def _domain_sources_app(make_app: Callable[..., FastAPI], supply: list[str]) -> FastAPI:
    return make_app(
        {"schema_inference": {"allowed_sources": [SOURCE], "domain_sources": {"supply": supply}}}
    )


@pytest.fixture
def fake_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ERP_DSN", "postgresql://reader@db.example.test/erp")
    monkeypatch.setenv("TEST_OTHER_DSN", "postgresql://reader@other.example.test/erp")

    async def profile(
        engine: object, settings: object, *, schema: str, tables: Any
    ) -> list[TableProfile]:
        return [profile_file(SUPPLY_DATA / "supplier.csv", CsvProfilingSettings())]

    monkeypatch.setattr(schema_inference, "profile_postgres", profile)


@pytest.mark.usefixtures("fake_profile")
async def test_a_domain_override_allows_only_its_own_sources(
    make_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    async with client_for(_domain_sources_app(make_app, [OTHER_SOURCE])) as http:
        own = await http.post(URL, json={"postgres": {"dsn": OTHER_SOURCE}})
        default = await http.post(URL, json={"postgres": {"dsn": SOURCE}})
    assert own.status_code == 200, own.text
    assert default.status_code == 422, default.text
    assert default.json()["errors"][0]["pointer"] == "/body/postgres/dsn"
    assert "TEST_OTHER_DSN" not in default.text


@pytest.mark.usefixtures("fake_profile")
async def test_a_domain_without_override_uses_the_default(
    make_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    async with client_for(_domain_sources_app(make_app, [OTHER_SOURCE])) as http:
        default = await http.post(OTHER_URL, json={"postgres": {"dsn": SOURCE}})
        foreign = await http.post(OTHER_URL, json={"postgres": {"dsn": OTHER_SOURCE}})
    assert default.status_code == 200, default.text
    assert foreign.status_code == 422, foreign.text
    assert "TEST_ERP_DSN" not in foreign.text


@pytest.mark.usefixtures("fake_profile")
async def test_an_empty_domain_override_refuses_every_source(
    make_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    async with client_for(_domain_sources_app(make_app, [])) as http:
        response = await http.post(URL, json={"postgres": {"dsn": SOURCE}})
    assert response.status_code == 422, response.text
    assert response.json()["errors"][0]["pointer"] == "/body/postgres/dsn"
