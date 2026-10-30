"""`POST /domains/{d}/schema:infer` with small data files and a Postgres source reference."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import date
from decimal import Decimal
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


def _parquet(rows: list[dict[str, Any]]) -> bytes:
    import pyarrow
    import pyarrow.parquet as parquet

    sink = io.BytesIO()
    parquet.write_table(pyarrow.Table.from_pylist(rows), sink)
    return sink.getvalue()


SUPPLIER_ROWS = [
    {"supplier_code": "S-1", "legal_name": "Kavsen", "country": "PL"},
    {"supplier_code": "S-2", "legal_name": "Sundello", "country": "PL"},
    {"supplier_code": "S-3", "legal_name": "Velkrix", "country": "CZ"},
]
ORDER_ROWS = [
    {"po_number": "PO-1", "supplier_id": "S-1", "qty": 5},
    {"po_number": "PO-2", "supplier_id": "S-1", "qty": 7},
    {"po_number": "PO-3", "supplier_id": "S-3", "qty": 1},
]


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


@pytest.mark.parametrize(
    ("supplier", "orders"),
    [
        (("supplier.json", json.dumps(SUPPLIER_ROWS).encode()), ("purchase_orders.csv", ORDERS)),
        (("supplier.csv", SUPPLIER), ("purchase_orders.jsonl", _jsonl(ORDER_ROWS))),
        (("supplier.ndjson", _jsonl(SUPPLIER_ROWS)), ("purchase_orders.json", b"[]")),
    ],
)
async def test_json_and_json_lines_files_give_a_proposal(
    client: httpx.AsyncClient, supplier: tuple[str, bytes], orders: tuple[str, bytes]
) -> None:
    response = await client.post(URL, files=files(supplier, orders))
    assert response.status_code == 200, response.text
    body = response.json()
    kinds = {k["api_name"] for k in body["schema"]["kinds"].values()}
    assert "Supplier" in kinds
    assert {t["table"] for t in body["tables"]} == {"supplier", "purchase_orders"}


async def test_json_values_keep_their_types(client: httpx.AsyncClient) -> None:
    response = await client.post(URL, files=files(("purchase_orders.jsonl", _jsonl(ORDER_ROWS))))
    assert response.status_code == 200, response.text
    (kind,) = response.json()["schema"]["kinds"].values()
    types = {a["api_name"]: a["type"]["name"] for a in kind["attributes"].values()}
    assert types["qty"] == "integer"


async def test_parquet_files_give_a_proposal(client: httpx.AsyncClient) -> None:
    uploads = files(
        ("supplier.parquet", _parquet(SUPPLIER_ROWS)),
        ("purchase_orders.parquet", _parquet(ORDER_ROWS)),
    )
    response = await client.post(URL, files=uploads)
    assert response.status_code == 200, response.text
    schema = response.json()["schema"]
    kinds = {k["api_name"] for k in schema["kinds"].values()}
    assert kinds == {"PurchaseOrder", "Supplier"}
    relations = [r["api_name"] for r in schema["relations"].values()]
    assert relations == ["supplier"]


async def test_parquet_without_pyarrow_is_refused(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _parquet(SUPPLIER_ROWS)
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    response = await client.post(URL, files=files(("supplier.parquet", data)))
    assert response.status_code == 422, response.text
    error = response.json()["errors"][0]
    assert error["pointer"] == "/body/files/0"
    assert "lorebase-keeper[parquet]" in error["message"]


@pytest.mark.parametrize(
    ("upload", "message"),
    [
        (("a.json", b"{not json"), "not valid JSON"),
        (("a.json", b'{"items": []}'), "not an array of records"),
        (("a.json", b"[1, 2]"), "record 1 is not an object"),
        (("a.jsonl", b'{"a": 1}\n[2]\n'), "line 2 is not an object"),
        (("a.jsonl", b'{"a": 1}\n{oops\n'), "line 2 is not valid JSON"),
        (("a.json", b"\xff\xfe["), "not UTF-8"),
        (("a.parquet", b"PAR1 not really"), "not a readable Parquet file"),
    ],
)
async def test_invalid_record_files(
    client: httpx.AsyncClient, upload: tuple[str, bytes], message: str
) -> None:
    response = await client.post(URL, files=files(upload))
    assert response.status_code == 422, response.text
    error = response.json()["errors"][0]
    assert error["pointer"] == "/body/files/0"
    assert message in error["message"]


async def test_file_formats_are_configured_by_suffix(
    make_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    app = make_app(
        {"schema_inference": {"file_suffixes": [".data"], "file_formats": {".DATA": "jsonl"}}}
    )
    async with client_for(app) as http:
        response = await http.post(URL, files=files(("orders.data", _jsonl(ORDER_ROWS))))
    assert response.status_code == 200, response.text
    assert [t["table"] for t in response.json()["tables"]] == ["orders"]


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


# JSON documents with records under a key ----------------------------------------------------


NESTED = json.dumps({"data": {"items": SUPPLIER_ROWS}, "total": 3}).encode()


async def test_records_path_locates_the_records_of_a_json_upload(
    client: httpx.AsyncClient,
) -> None:
    uploads = files(("supplier.json", NESTED))
    refused = await client.post(URL, files=uploads)
    assert refused.status_code == 422, refused.text
    assert "give records_path" in refused.json()["errors"][0]["message"]
    response = await client.post(URL, files=uploads, data={"records_path": "data.items"})
    assert response.status_code == 200, response.text
    (table,) = response.json()["tables"]
    assert table["table"] == "supplier"


@pytest.mark.parametrize(
    ("path", "message"),
    [("data.rows", "no 'data.rows' in the document"), ("data..items", "dotted keys")],
)
async def test_invalid_records_path(client: httpx.AsyncClient, path: str, message: str) -> None:
    response = await client.post(
        URL, files=files(("supplier.json", NESTED)), data={"records_path": path}
    )
    assert response.status_code == 422, response.text
    assert message in response.json()["errors"][0]["message"]


# MySQL, MariaDB and SQL Server sources -------------------------------------------------------


@pytest.mark.parametrize("key", ["mysql", "mssql"])
async def test_sql_source_gives_a_proposal(
    make_app: Callable[..., FastAPI],
    client_for: ClientFor,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    monkeypatch.setenv("TEST_ERP_DSN", f"{key}://reader@db.example.test/erp")
    seen: dict[str, Any] = {}

    async def fake_profile(
        dialect: str,
        url: str,
        sql: object,
        records: object,
        *,
        application: str,
        schema: Any,
        tables: Any,
        allowed_schemas: Any,
    ) -> list[TableProfile]:
        seen.update(dialect=dialect, url=url, application=application, schema=schema, tables=tables)
        csv = CsvProfilingSettings()
        return [profile_file(SUPPLY_DATA / f"{name}.csv", csv) for name in ("supplier", "contract")]

    monkeypatch.setattr(schema_inference, "profile_sql_database", fake_profile)
    async with client_for(_source_app(make_app)) as http:
        response = await http.post(
            URL, json={key: {"dsn": SOURCE, "schema": "erp", "tables": ["supplier"]}}
        )
    assert response.status_code == 200, response.text
    assert seen == {
        "dialect": key,
        "url": f"{key}://reader@db.example.test/erp",
        "application": "lorebase-keeper",
        "schema": "erp",
        "tables": ("supplier",),
    }
    kinds = {k["api_name"] for k in response.json()["schema"]["kinds"].values()}
    assert kinds == {"Contract", "Supplier"}


@pytest.mark.parametrize(
    ("body", "pointer"),
    [
        ({"mssql": {"dsn": "mssql://u:p@db/erp"}}, "/body/mssql/dsn"),
        ({"mysql": {"dsn": "secret://env/OTHER"}}, "/body/mysql/dsn"),
        ({"mysql": {"dsn": SOURCE}}, "/body/mysql/dsn"),  # not set in the environment
        ({"mssql": {"dsn": SOURCE}, "mysql": {"dsn": SOURCE}}, "/body"),
        ({"options": {}}, "/body"),
    ],
)
async def test_invalid_sql_sources(
    make_app: Callable[..., FastAPI], client_for: ClientFor, body: Any, pointer: str
) -> None:
    async with client_for(_source_app(make_app)) as http:
        response = await http.post(URL, json=body)
    assert response.status_code == 422, response.text
    assert response.json()["errors"][0]["pointer"] == pointer
    assert "mssql://" not in response.text


async def test_sql_source_errors_do_not_leak_the_url(
    make_app: Callable[..., FastAPI], client_for: ClientFor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_ERP_DSN", "mssql://reader:hunter2-long@db.example.test/erp")

    async def unavailable(*_: Any, **__: Any) -> list[TableProfile]:
        raise OSError("connection to mssql://reader:hunter2-long@db refused")

    async def unknown(*_: Any, **__: Any) -> list[TableProfile]:
        raise schema_inference.SqlProfileError("unknown tables in the database: nope")

    async with client_for(_source_app(make_app)) as http:
        monkeypatch.setattr(schema_inference, "profile_sql_database", unavailable)
        down = await http.post(URL, json={"mssql": {"dsn": SOURCE}})
        monkeypatch.setattr(schema_inference, "profile_sql_database", unknown)
        missing = await http.post(URL, json={"mssql": {"dsn": SOURCE}})
    assert down.status_code == 503, down.text
    assert "hunter2" not in down.text
    assert missing.status_code == 422, missing.text
    assert missing.json()["errors"][0]["pointer"] == "/body/mssql"


# Preview ----------------------------------------------------------------------------------

PREVIEW = "/api/v1/domains/supply/schema:preview"


async def test_preview_returns_the_first_rows_of_a_parquet_file(
    client: httpx.AsyncClient,
) -> None:
    rows = [{"id": n, "day": date(2026, 1, n), "price": Decimal("1.50")} for n in range(1, 8)]
    response = await client.post(
        PREVIEW, files=files(("days.parquet", _parquet(rows))), params={"rows": 3}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["domain"] == "supply"
    (preview,) = body["files"]
    assert preview["name"] == "days.parquet"
    assert preview["columns"] == ["id", "day", "price"]
    assert preview["rows"] == [
        {"id": n, "day": f"2026-01-0{n}", "price": "1.50"} for n in (1, 2, 3)
    ]
    assert (preview["row_count"], preview["truncated"]) == (7, True)


async def test_preview_reads_text_formats_and_records_paths(client: httpx.AsyncClient) -> None:
    response = await client.post(
        PREVIEW, files=files(("supplier.csv", SUPPLIER), ("orders.jsonl", _jsonl(ORDER_ROWS)))
    )
    assert response.status_code == 200, response.text
    csv, jsonl = response.json()["files"]
    assert csv["rows"][0] == {"supplier_code": "S-1", "legal_name": "Kavsen", "country": "PL"}
    assert jsonl["rows"][1] == ORDER_ROWS[1]
    assert (csv["truncated"], jsonl["truncated"]) == (False, False)
    nested = await client.post(
        PREVIEW, files=files(("nested.json", NESTED)), data={"records_path": "data.items"}
    )
    assert nested.status_code == 200, nested.text
    assert nested.json()["files"][0]["rows"][0] == SUPPLIER_ROWS[0]


@pytest.mark.parametrize("rows", [0, 201])
async def test_preview_rows_are_bounded(client: httpx.AsyncClient, rows: int) -> None:
    response = await client.post(PREVIEW, files=small(), params={"rows": rows})
    assert response.status_code == 422, response.text
    assert response.json()["errors"][0]["pointer"] == "/query/rows"


async def test_preview_obeys_the_upload_limits(
    make_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    app = make_app({"schema_inference": {"max_files": 1, "max_file_bytes": 1000}})
    async with client_for(app) as http:
        too_many = await http.post(PREVIEW, files=small())
        too_big = await http.post(PREVIEW, files=files(("a.csv", b"x\n" + b"1\n" * 600)))
        suffix = await http.post(PREVIEW, files=files(("a.xlsx", SUPPLIER)))
        json_body = await http.post(PREVIEW, json={"postgres": {"dsn": SOURCE}})
    assert too_many.status_code == 422
    assert too_big.status_code == 413
    assert suffix.status_code == 422
    assert json_body.status_code == 422


async def test_preview_without_pyarrow_is_refused(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _parquet(SUPPLIER_ROWS)
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    response = await client.post(PREVIEW, files=files(("supplier.parquet", data)))
    assert response.status_code == 422, response.text
    assert "lorebase-keeper[parquet]" in response.json()["errors"][0]["message"]


# Hardening of database sources and previews ---------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        schema_inference.SqlDriverError(
            "mssql sources need pymssql: install lorebase-keeper[mssql]"
        ),
        RuntimeError("driver exploded on mssql://reader:hunter2-long@db"),
    ],
)
async def test_server_side_failures_are_a_generic_503(
    make_app: Callable[..., FastAPI],
    client_for: ClientFor,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    monkeypatch.setenv("TEST_ERP_DSN", "mssql://reader:hunter2-long@db.example.test/erp")

    async def broken(*_: Any, **__: Any) -> list[TableProfile]:
        raise failure

    monkeypatch.setattr(schema_inference, "profile_sql_database", broken)
    async with client_for(_source_app(make_app)) as http:
        response = await http.post(URL, json={"mssql": {"dsn": SOURCE}})
    assert response.status_code == 503, response.text
    assert "lorebase-keeper[" not in response.text
    assert "hunter2" not in response.text


async def test_allowed_schemas_of_a_source_reach_the_profiler(
    make_app: Callable[..., FastAPI], client_for: ClientFor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_ERP_DSN", "mssql://reader@db.example.test/erp")
    seen: list[Any] = []

    async def fake_profile(*_: Any, allowed_schemas: Any, **__: Any) -> list[TableProfile]:
        seen.append(allowed_schemas)
        return [profile_file(SUPPLY_DATA / "supplier.csv", CsvProfilingSettings())]

    monkeypatch.setattr(schema_inference, "profile_sql_database", fake_profile)
    app = make_app(
        {"schema_inference": {"allowed_sources": [SOURCE], "allowed_schemas": {SOURCE: ["dbo"]}}}
    )
    async with client_for(app) as http:
        await http.post(URL, json={"mssql": {"dsn": SOURCE}})
        unlisted = make_app({"schema_inference": {"allowed_sources": [SOURCE]}})
    async with client_for(unlisted) as http:
        await http.post(URL, json={"mssql": {"dsn": SOURCE}})
    assert seen == [("dbo",), None]


async def test_table_lists_are_bounded(
    make_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    async with client_for(_source_app(make_app)) as http:
        too_many = await http.post(
            URL, json={"mssql": {"dsn": SOURCE, "tables": [f"t{i}" for i in range(1001)]}}
        )
        too_long = await http.post(URL, json={"mssql": {"dsn": SOURCE, "tables": ["t" * 129]}})
    assert too_many.status_code == too_long.status_code == 422


async def test_preview_cuts_long_text(
    make_app: Callable[..., FastAPI], client_for: ClientFor
) -> None:
    app = make_app({"schema_inference": {"preview_max_cell_chars": 10}})
    long = _jsonl([{"id": 1, "note": "n" * 50, "nested": {"deep": "d" * 50}}])
    async with client_for(app) as http:
        response = await http.post(PREVIEW, files=files(("a.jsonl", long)))
    assert response.status_code == 200, response.text
    row = response.json()["files"][0]["rows"][0]
    assert row["note"] == "n" * 10 + "..."
    assert row["nested"] == {"deep": "d" * 10 + "..."}


async def test_records_path_accepts_a_bare_array(client: httpx.AsyncClient) -> None:
    response = await client.post(
        PREVIEW,
        files=files(("s.json", json.dumps(SUPPLIER_ROWS).encode())),
        data={"records_path": "data.items"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["files"][0]["row_count"] == 3
