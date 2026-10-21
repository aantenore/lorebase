"""CSV and Postgres connectors without a network or a database."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from lorebase_core.model import Schema, Source
from lorebase_core.model.source import SourceType, SyncMode
from lorebase_scribe.settings import CsvSourceSettings, PostgresSourceSettings
from lorebase_scribe.sources.connectors import (
    CsvConnector,
    PostgresConnector,
    SourceReadError,
)
from lorebase_scribe.sources.mapping import SourceDefinitionError

CSV = b"supplier_code;legal_name;updated\nS-1;Acme;2026-01-01\n\nS-2;Beta;2026-02-01\n"


def csv_source(schema: Schema, monkeypatch: pytest.MonkeyPatch, location: str) -> Source:
    monkeypatch.setenv("TEST_CSV_LOCATION", location)
    found = schema.source_by_api_name("approved.suppliers")
    assert found is not None
    return found.model_copy(update={"connection": "secret://env/TEST_CSV_LOCATION"})


async def collect(connector: Any, since: Any = None) -> list[tuple[str, dict[str, Any]]]:
    return [(locator, dict(row)) async for locator, row in connector.rows(since)]


async def test_local_file_under_an_allowed_root(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "suppliers.csv"
    path.write_bytes(CSV)
    source = csv_source(supply_schema, monkeypatch, str(path))
    connector = CsvConnector(source, CsvSourceSettings(allowed_roots=(str(tmp_path),)))
    rows = await collect(connector)
    assert connector.columns == ("supplier_code", "legal_name", "updated")
    assert rows == [
        (
            "suppliers.csv:2",
            {"supplier_code": "S-1", "legal_name": "Acme", "updated": "2026-01-01"},
        ),
        (
            "suppliers.csv:4",
            {"supplier_code": "S-2", "legal_name": "Beta", "updated": "2026-02-01"},
        ),
    ]


async def test_incremental_csv_reads_rows_after_the_watermark(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "suppliers.csv"
    path.write_bytes(CSV)
    source = csv_source(supply_schema, monkeypatch, str(path)).model_copy(
        update={"sync_mode": SyncMode.INCREMENTAL, "watermark_column": "updated"}
    )
    connector = CsvConnector(source, CsvSourceSettings(allowed_roots=(str(tmp_path),)))
    rows = await collect(connector, "2026-01-15")
    assert [row["supplier_code"] for _, row in rows] == ["S-2"]


async def test_files_outside_the_allowed_roots_are_refused(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "suppliers.csv"
    path.write_bytes(CSV)
    source = csv_source(supply_schema, monkeypatch, str(path))
    with pytest.raises(SourceDefinitionError, match="allowed_roots"):
        await collect(CsvConnector(source, CsvSourceSettings()))
    escape = csv_source(supply_schema, monkeypatch, str(tmp_path / "sub" / ".." / ".." / "x.csv"))
    with pytest.raises(SourceDefinitionError, match="allowed_roots"):
        await collect(CsvConnector(escape, CsvSourceSettings(allowed_roots=(str(tmp_path),))))
    source = csv_source(supply_schema, monkeypatch, str(path))
    small = CsvSourceSettings(allowed_roots=(str(tmp_path),), max_bytes=10)
    with pytest.raises(SourceDefinitionError, match="bytes"):
        await collect(CsvConnector(source, small))


def transport(status: int = 200, body: bytes = CSV, seen: list[str] | None = None) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        headers = {"location": "https://elsewhere.test/x.csv"} if status == 302 else {}
        return httpx.Response(status, content=body, headers=headers)

    return httpx.MockTransport(handle)


async def test_download_from_an_allowed_host(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = csv_source(supply_schema, monkeypatch, "https://files.example.test/suppliers.csv")
    settings = CsvSourceSettings(allowed_hosts=("files.example.test",))
    seen: list[str] = []
    rows = await collect(CsvConnector(source, settings, transport(seen=seen)))
    assert [locator for locator, _ in rows] == ["suppliers.csv:2", "suppliers.csv:4"]
    assert seen == ["https://files.example.test/suppliers.csv"]


async def test_download_refusals(supply_schema: Schema, monkeypatch: pytest.MonkeyPatch) -> None:
    allowed = CsvSourceSettings(allowed_hosts=("files.example.test",))
    other = csv_source(supply_schema, monkeypatch, "https://other.test/suppliers.csv")
    with pytest.raises(SourceDefinitionError, match="allowed_hosts"):
        await collect(CsvConnector(other, allowed, transport()))
    plain = csv_source(supply_schema, monkeypatch, "http://files.example.test/suppliers.csv")
    with pytest.raises(SourceDefinitionError, match="allowed_schemes"):
        await collect(CsvConnector(plain, allowed, transport()))
    good = csv_source(supply_schema, monkeypatch, "https://files.example.test/suppliers.csv")
    with pytest.raises(SourceReadError, match="302"):
        await collect(CsvConnector(good, allowed, transport(status=302)))
    tiny = CsvSourceSettings(allowed_hosts=("files.example.test",), max_bytes=10)
    with pytest.raises(SourceDefinitionError, match="larger"):
        await collect(CsvConnector(good, tiny, transport()))


def postgres_source(schema: Schema, **changes: Any) -> Source:
    found = schema.source_by_api_name("erp.purchase_orders")
    assert found is not None
    return found.model_copy(update=changes)


def test_table_statement_defaults_to_the_api_name(supply_schema: Schema) -> None:
    connector = PostgresConnector(postgres_source(supply_schema), PostgresSourceSettings())
    sql, parameters, name = connector.statement(None)
    assert sql == (
        'SELECT * FROM "purchase_orders" AS lorebase_source '
        "WHERE (state <> 'archived') ORDER BY \"updated_at\""
    )
    assert parameters == {}
    assert name == "purchase_orders"
    sql, parameters, _ = connector.statement("2026-01-01")
    assert sql.endswith('AND "updated_at" > :since ORDER BY "updated_at"')
    assert parameters == {"since": "2026-01-01"}


def test_query_and_qualified_table_statements(supply_schema: Schema) -> None:
    table = PostgresConnector(
        postgres_source(supply_schema, table="erp.orders", scope_filter=None),
        PostgresSourceSettings(),
    )
    assert table.statement(None)[0].startswith('SELECT * FROM "erp"."orders" AS lorebase_source')
    query = PostgresConnector(
        postgres_source(
            supply_schema,
            type=SourceType.POSTGRES_QUERY,
            query="SELECT * FROM orders;",
            scope_filter=None,
            watermark_column=None,
            sync_mode=SyncMode.SNAPSHOT,
        ),
        PostgresSourceSettings(),
    )
    assert query.statement(None) == (
        "SELECT * FROM (SELECT * FROM orders) AS lorebase_source",
        {},
        "query",
    )
    bad = PostgresConnector(
        postgres_source(supply_schema, table='orders"; DROP TABLE x; --'),
        PostgresSourceSettings(),
    )
    with pytest.raises(SourceDefinitionError, match="identifier"):
        bad.statement(None)
