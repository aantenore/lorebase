"""MySQL and SQL Server Sources against real servers (testcontainers; skipped without Docker).

Images: `LOREBASE_TEST_MYSQL_IMAGE` (default mysql:8.0) and `LOREBASE_TEST_MSSQL_IMAGE` (default
mcr.microsoft.com/mssql/server:2022-latest, an amd64 image: emulated on ARM hosts).
"""

from __future__ import annotations

import os
import platform
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from lorebase_core.model import Schema, Source
from lorebase_core.model.source import SourceType, SyncMode
from lorebase_scribe.settings import MssqlSourceSettings, MysqlSourceSettings, SourceSettings
from lorebase_scribe.sources import (
    MssqlConnector,
    MysqlConnector,
    SourceReadError,
    profile_source,
)
from lorebase_store.settings import RecordProfilingSettings

pytestmark = pytest.mark.integration

CONNECTION = "secret://env/TEST_SQL_DSN"
MYSQL_IMAGE = os.environ.get("LOREBASE_TEST_MYSQL_IMAGE", "mysql:8.0")
MSSQL_IMAGE = os.environ.get(
    "LOREBASE_TEST_MSSQL_IMAGE", "mcr.microsoft.com/mssql/server:2022-latest"
)
PASSWORD = "Lorebase-Test-1!"

TABLE = {
    "mysql": (
        "CREATE TABLE purchase_orders (po_number VARCHAR(20) PRIMARY KEY, "
        "total DECIMAL(12,2) NOT NULL, state VARCHAR(20) NOT NULL, "
        "updated_at DATETIME(6) NOT NULL)"
    ),
    "mssql": (
        "CREATE TABLE purchase_orders (po_number VARCHAR(20) PRIMARY KEY, "
        "total DECIMAL(12,2) NOT NULL, state VARCHAR(20) NOT NULL, "
        "updated_at DATETIME2(6) NOT NULL)"
    ),
}
INSERT = "INSERT INTO purchase_orders (po_number, total, state, updated_at) VALUES (:n, :t, :s, :u)"
ORDERS = [
    ("PO-1", Decimal("10.50"), "open", datetime(2026, 1, 1, 8, 0, 0, 1)),
    ("PO-2", Decimal("20.00"), "archived", datetime(2026, 1, 2, 8, 0, 0)),
    ("PO-3", Decimal("30.25"), "open", datetime(2026, 1, 3, 8, 0, 0)),
    ("PO-4", Decimal("40.00"), "open", datetime(2026, 1, 4, 8, 0, 0)),
    ("PO-5", Decimal("50.00"), "open", datetime(2026, 1, 5, 8, 0, 0)),
]


@dataclass(frozen=True)
class Server:
    kind: str
    dsn: str
    admin_url: str
    source_type: SourceType
    query_type: SourceType
    connector: Any
    settings: Any


def _docker() -> None:
    try:
        import docker  # type: ignore[import-untyped]

        docker.from_env().ping()
    except Exception as error:  # any failure means no usable Docker
        pytest.skip(f"Docker is not available: {type(error).__name__}")


def _seed(admin_url: str, kind: str) -> None:
    engine = create_engine(admin_url)
    try:
        with engine.begin() as connection:
            connection.execute(text(TABLE[kind]))
            for number, total, state, updated in ORDERS:
                connection.execute(
                    text(INSERT),
                    {"n": number, "t": total, "s": state, "u": updated.isoformat(sep=" ")},
                )
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def mysql_server() -> Iterator[Server]:
    _docker()
    from testcontainers.community.mysql import MySqlContainer

    with MySqlContainer(
        MYSQL_IMAGE, username="lorebase", password=PASSWORD, dbname="erp", root_password=PASSWORD
    ) as container:
        host, port = container.get_container_host_ip(), container.get_exposed_port(3306)
        admin = f"mysql+pymysql://lorebase:{PASSWORD}@{host}:{port}/erp"
        _seed(admin, "mysql")
        yield Server(
            "mysql",
            f"mysql://lorebase:{PASSWORD}@{host}:{port}/erp",
            admin,
            SourceType.MYSQL_TABLE,
            SourceType.MYSQL_QUERY,
            MysqlConnector,
            MysqlSourceSettings(fetch_rows=2, connect_timeout_s=30),
        )


@pytest.fixture(scope="module")
def mssql_server() -> Iterator[Server]:
    _docker()
    from testcontainers.community.mssql import SqlServerContainer

    options: dict[str, Any] = {}
    if platform.machine() in ("arm64", "aarch64"):
        options["platform"] = "linux/amd64"
    with SqlServerContainer(
        MSSQL_IMAGE, password=PASSWORD, dbname="master", **options
    ) as container:
        host, port = container.get_container_host_ip(), container.get_exposed_port(1433)
        admin = f"mssql+pymssql://sa:{PASSWORD}@{host}:{port}/master"
        _seed(admin, "mssql")
        yield Server(
            "mssql",
            f"mssql://sa:{PASSWORD}@{host}:{port}/master",
            admin,
            SourceType.MSSQL_TABLE,
            SourceType.MSSQL_QUERY,
            MssqlConnector,
            MssqlSourceSettings(fetch_rows=2, connect_timeout_s=30),
        )


@pytest.fixture(params=["mysql", "mssql"])
def server(request: pytest.FixtureRequest) -> Server:
    found = request.getfixturevalue(f"{request.param}_server")
    assert isinstance(found, Server)
    return found


def source_of(
    schema: Schema, monkeypatch: pytest.MonkeyPatch, server: Server, **changes: Any
) -> Source:
    monkeypatch.setenv("TEST_SQL_DSN", server.dsn)
    found = schema.source_by_api_name("erp.purchase_orders")
    assert found is not None
    update = {
        "type": server.source_type,
        "connection": CONNECTION,
        "scope_filter": None,
        "sync_mode": SyncMode.INCREMENTAL,
        "watermark_column": "updated_at",
    }
    return found.model_copy(update={**update, **changes})


async def collect(connector: Any, since: Any = None) -> list[tuple[str, dict[str, Any]]]:
    return [(locator, dict(row)) async for locator, row in connector.rows(since)]


async def test_a_table_is_streamed_in_watermark_order(
    server: Server, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_of(supply_schema, monkeypatch, server)
    connector = server.connector(source, server.settings)
    rows = await collect(connector)
    assert [locator for locator, _ in rows] == [f"purchase_orders:{n}" for n in range(1, 6)]
    assert [row["po_number"] for _, row in rows] == [order[0] for order in ORDERS]
    assert connector.columns == ("po_number", "total", "state", "updated_at")
    assert rows[0][1]["total"] == Decimal("10.50")
    assert rows[0][1]["updated_at"] == ORDERS[0][3]


async def test_an_incremental_run_reads_rows_past_the_watermark(
    server: Server, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_of(supply_schema, monkeypatch, server)
    connector = server.connector(source, server.settings)
    rows = await collect(connector, datetime(2026, 1, 3, 8, 0, 0))
    assert [row["po_number"] for _, row in rows] == ["PO-4", "PO-5"]
    assert await collect(connector, datetime(2026, 1, 5, 8, 0, 0)) == []
    # The watermark keeps its microseconds: the row at that very instant is not read again.
    rows = await collect(connector, ORDERS[0][3])
    assert [row["po_number"] for _, row in rows] == ["PO-2", "PO-3", "PO-4", "PO-5"]


async def test_scope_filter_and_query_sources(
    server: Server, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = source_of(supply_schema, monkeypatch, server, scope_filter="state <> 'archived'")
    rows = await collect(server.connector(table, server.settings))
    assert [row["po_number"] for _, row in rows] == ["PO-1", "PO-3", "PO-4", "PO-5"]
    query = source_of(
        supply_schema,
        monkeypatch,
        server,
        type=server.query_type,
        query="SELECT po_number, total * 2 AS doubled, updated_at FROM purchase_orders;",
    )
    connector = server.connector(query, server.settings)
    rows = await collect(connector, datetime(2026, 1, 4, 8, 0, 0))
    assert [locator for locator, _ in rows] == ["query:1"]
    assert rows[0][1]["doubled"] == Decimal("100.00")
    assert connector.columns == ("po_number", "doubled", "updated_at")


async def test_a_wrong_password_is_a_read_error_without_the_password(
    server: Server, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_of(supply_schema, monkeypatch, server)
    monkeypatch.setenv("TEST_SQL_DSN", server.dsn.replace(PASSWORD, "wrong-password-9"))
    with pytest.raises(SourceReadError) as caught:
        await collect(server.connector(source, server.settings))
    assert "wrong-password-9" not in str(caught.value)


async def test_a_missing_table_is_a_read_error(
    server: Server, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_of(supply_schema, monkeypatch, server, table="no_such_table")
    with pytest.raises(SourceReadError, match="no_such_table"):
        await collect(server.connector(source, server.settings))


async def test_a_table_is_profiled_for_inference(
    server: Server, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_of(supply_schema, monkeypatch, server)
    settings = SourceSettings(allowed_connections=(CONNECTION,), **{server.kind: server.settings})
    profile = await profile_source(source, settings, RecordProfilingSettings())
    assert profile.name == "purchase_orders"
    assert profile.row_count == 5
    assert [column.name for column in profile.columns] == [
        "po_number",
        "total",
        "state",
        "updated_at",
    ]
