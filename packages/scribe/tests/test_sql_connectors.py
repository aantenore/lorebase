"""MySQL/MariaDB and SQL Server Sources, read through fake engines."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from lorebase_core.model import Schema, Source
from lorebase_core.model.source import SourceType, SyncMode
from lorebase_scribe.settings import (
    MssqlSourceSettings,
    MysqlSourceSettings,
    SourceSettings,
)
from lorebase_scribe.sources import (
    MssqlConnector,
    MysqlConnector,
    SourceReadError,
    default_connector,
    profile_source,
)
from lorebase_scribe.sources import sql as sql_module
from lorebase_scribe.sources.mapping import SourceDefinitionError
from lorebase_scribe.sources.sql import mssql_orderable
from lorebase_scribe.sources.sync import decode_watermark, encode_watermark
from lorebase_store.settings import RecordProfilingSettings

CONNECTION = "secret://env/TEST_SQL_DSN"
ROWS = [{"po_number": f"PO-{n}", "total": n * 10} for n in range(1, 6)]


def erp_source(schema: Schema, monkeypatch: pytest.MonkeyPatch, dsn: str, **changes: Any) -> Source:
    monkeypatch.setenv("TEST_SQL_DSN", dsn)
    found = schema.source_by_api_name("erp.purchase_orders")
    assert found is not None
    return found.model_copy(update={"connection": CONNECTION, **changes})


async def collect(connector: Any, since: Any = None) -> list[tuple[str, dict[str, Any]]]:
    return [(locator, dict(row)) async for locator, row in connector.rows(since)]


# Fakes ------------------------------------------------------------------------------------


class FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.batches: list[int] = []

    def keys(self) -> list[str]:
        return list(self._rows[0]) if self._rows else []

    def mappings(self) -> FakeResult:
        return self

    async def partitions(self, size: int) -> AsyncIterator[list[dict[str, Any]]]:
        for start in range(0, len(self._rows), size):
            self.batches.append(size)
            yield self._rows[start : start + size]

    def fetchmany(self, size: int) -> list[dict[str, Any]]:
        self.batches.append(size)
        batch, self._rows = self._rows[:size], self._rows[size:]
        return batch


class FakeAsyncConnection:
    def __init__(self, log: list[Any], rows: list[dict[str, Any]], mariadb: bool) -> None:
        self.log, self.rows = log, rows
        self.dialect = SimpleNamespace(is_mariadb=mariadb)

    async def execute(self, statement: Any) -> None:
        self.log.append(str(statement))

    async def rollback(self) -> None:
        self.log.append("ROLLBACK")

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        self.log.append("BEGIN")
        yield
        self.log.append("END")

    async def stream(self, statement: Any, parameters: dict[str, Any]) -> FakeResult:
        self.log.append((str(statement), parameters))
        return FakeResult(list(self.rows))


class FakeAsyncEngine:
    def __init__(self, log: list[Any], rows: list[dict[str, Any]], mariadb: bool) -> None:
        self.log, self.rows, self.mariadb = log, rows, mariadb
        self.disposed = False

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[FakeAsyncConnection]:
        yield FakeAsyncConnection(self.log, self.rows, self.mariadb)

    async def dispose(self) -> None:
        self.disposed = True


class FakeSyncConnection:
    def __init__(self, log: list[Any], rows: list[dict[str, Any]]) -> None:
        self.log, self.rows, self.closed = log, rows, False

    def execute(self, statement: Any, parameters: dict[str, Any] | None = None) -> FakeResult:
        self.log.append((str(statement), parameters))
        return FakeResult(list(self.rows))

    def close(self) -> None:
        self.closed = True


class FakeSyncEngine:
    def __init__(self, log: list[Any], rows: list[dict[str, Any]]) -> None:
        self.log, self.rows, self.disposed = log, rows, False
        self.connection: FakeSyncConnection | None = None

    def connect(self) -> FakeSyncConnection:
        self.connection = FakeSyncConnection(self.log, self.rows)
        return self.connection

    def dispose(self) -> None:
        self.disposed = True


@pytest.fixture
def mysql_engine(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the engine factory of MySQL Sources; `made` records the engines and options."""
    made: dict[str, Any] = {"log": [], "rows": ROWS, "mariadb": False, "options": []}

    def factory(url: Any, **options: Any) -> FakeAsyncEngine:
        made["options"].append((url, options))
        made["engine"] = FakeAsyncEngine(made["log"], made["rows"], made["mariadb"])
        return made["engine"]  # type: ignore[no-any-return]

    monkeypatch.setattr(MysqlConnector, "engine_factory", staticmethod(factory))
    return made


@pytest.fixture
def mssql_engine(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    made: dict[str, Any] = {"log": [], "rows": ROWS, "options": []}

    def factory(url: Any, **options: Any) -> FakeSyncEngine:
        made["options"].append((url, options))
        made["engine"] = FakeSyncEngine(made["log"], made["rows"])
        return made["engine"]  # type: ignore[no-any-return]

    monkeypatch.setattr(MssqlConnector, "engine_factory", staticmethod(factory))
    return made


# Statements -------------------------------------------------------------------------------


def test_mysql_statements_quote_with_backticks(supply_schema: Schema) -> None:
    source = supply_schema.source_by_api_name("erp.purchase_orders")
    assert source is not None
    source = source.model_copy(update={"type": SourceType.MYSQL_TABLE, "scope_filter": None})
    connector = MysqlConnector(source, MysqlSourceSettings())
    sql, parameters, name = connector.statement(None)
    assert sql == "SELECT * FROM `purchase_orders` AS lorebase_source ORDER BY `updated_at`"
    assert (parameters, name) == ({}, "purchase_orders")
    sql, parameters, _ = connector.statement("2026-01-01")
    assert sql.endswith("WHERE `updated_at` > :since ORDER BY `updated_at`")
    assert parameters == {"since": "2026-01-01"}


def test_mssql_statements_quote_with_brackets_and_qualify(supply_schema: Schema) -> None:
    source = supply_schema.source_by_api_name("erp.purchase_orders")
    assert source is not None
    source = source.model_copy(
        update={"type": SourceType.MSSQL_TABLE, "table": "dbo.orders", "scope_filter": "x > 1"}
    )
    sql, _, name = MssqlConnector(source, MssqlSourceSettings()).statement(None)
    assert sql == (
        "SELECT * FROM [dbo].[orders] AS lorebase_source WHERE (x > 1) ORDER BY [updated_at]"
    )
    assert name == "dbo.orders"
    _, parameters, _ = MssqlConnector(source, MssqlSourceSettings()).statement(
        datetime(2026, 1, 2, 3, 4, 5, 6)
    )
    assert parameters == {"since": "2026-01-02 03:04:05.000006"}


@pytest.mark.parametrize(
    ("connector_type", "source_type", "settings"),
    [
        (MysqlConnector, SourceType.MYSQL_QUERY, MysqlSourceSettings()),
        (MssqlConnector, SourceType.MSSQL_QUERY, MssqlSourceSettings()),
    ],
)
def test_query_statements(
    supply_schema: Schema, connector_type: Any, source_type: SourceType, settings: Any
) -> None:
    base = supply_schema.source_by_api_name("erp.purchase_orders")
    assert base is not None
    changes: dict[str, Any] = {
        "type": source_type,
        "scope_filter": None,
        "watermark_column": None,
        "sync_mode": SyncMode.SNAPSHOT,
    }
    query = connector_type(
        base.model_copy(update={**changes, "query": "SELECT 1 AS a ;\n"}), settings
    )
    assert query.statement(None) == (
        "SELECT * FROM (SELECT 1 AS a) AS lorebase_source",
        {},
        "query",
    )
    blank = connector_type(base.model_copy(update={**changes, "query": " ; "}), settings)
    with pytest.raises(SourceDefinitionError, match="need a query"):
        blank.statement(None)


@pytest.mark.parametrize("connector_type", [MysqlConnector, MssqlConnector])
def test_identifiers_are_checked(supply_schema: Schema, connector_type: Any) -> None:
    base = supply_schema.source_by_api_name("erp.purchase_orders")
    assert base is not None
    source = base.model_copy(update={"table": "orders`]; DROP TABLE x; --"})
    with pytest.raises(SourceDefinitionError, match="identifier"):
        connector_type(source, MysqlSourceSettings()).statement(None)


# Factory and connection rules ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_type", "expected"),
    [
        (SourceType.MYSQL_TABLE, MysqlConnector),
        (SourceType.MYSQL_QUERY, MysqlConnector),
        (SourceType.MSSQL_TABLE, MssqlConnector),
        (SourceType.MSSQL_QUERY, MssqlConnector),
    ],
)
def test_default_connector_by_type(
    supply_schema: Schema, source_type: SourceType, expected: type
) -> None:
    base = supply_schema.source_by_api_name("erp.purchase_orders")
    assert base is not None
    source = base.model_copy(update={"type": source_type, "connection": CONNECTION})
    settings = SourceSettings(allowed_connections=(CONNECTION,))
    assert type(default_connector(source, settings)) is expected
    with pytest.raises(SourceDefinitionError, match="allowed_connections"):
        default_connector(source, SourceSettings())


async def test_a_missing_driver_names_the_extra(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(sql_module.importlib, "import_module", missing)
    mysql = erp_source(supply_schema, monkeypatch, "mysql://u:p@h/db", type=SourceType.MYSQL_TABLE)
    with pytest.raises(SourceDefinitionError, match=r"asyncmy: install lorebase-scribe\[mysql\]"):
        await collect(MysqlConnector(mysql, MysqlSourceSettings()))
    mssql = erp_source(supply_schema, monkeypatch, "mssql://u:p@h/db", type=SourceType.MSSQL_TABLE)
    with pytest.raises(SourceDefinitionError, match=r"pymssql: install lorebase-scribe\[mssql\]"):
        await collect(MssqlConnector(mssql, MssqlSourceSettings()))


@pytest.mark.parametrize(
    ("connector_type", "source_type", "dsn", "settings"),
    [
        (
            MysqlConnector,
            SourceType.MYSQL_TABLE,
            "postgresql://u:topsecret@h/db",
            MysqlSourceSettings(),
        ),
        (MssqlConnector, SourceType.MSSQL_TABLE, "mysql://u:topsecret@h/db", MssqlSourceSettings()),
        (MssqlConnector, SourceType.MSSQL_TABLE, "not a url, topsecret", MssqlSourceSettings()),
    ],
)
async def test_other_schemes_and_malformed_values_are_refused_without_echo(
    supply_schema: Schema,
    monkeypatch: pytest.MonkeyPatch,
    connector_type: Any,
    source_type: SourceType,
    dsn: str,
    settings: Any,
) -> None:
    source = erp_source(supply_schema, monkeypatch, dsn, type=source_type)
    with pytest.raises(SourceDefinitionError, match="connection:") as caught:
        await collect(connector_type(source, settings))
    assert "topsecret" not in str(caught.value)


# Reading ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mariadb", "limit"),
    [
        (False, "SET SESSION max_execution_time = 1500"),
        (True, "SET SESSION max_statement_time = 2"),
    ],
)
async def test_mysql_rows_stream_in_a_read_only_session(
    supply_schema: Schema,
    monkeypatch: pytest.MonkeyPatch,
    mysql_engine: dict[str, Any],
    mariadb: bool,
    limit: str,
) -> None:
    mysql_engine["mariadb"] = mariadb
    source = erp_source(
        supply_schema, monkeypatch, "mysql://u:p@db:3306/erp", type=SourceType.MYSQL_TABLE
    )
    connector = MysqlConnector(source, MysqlSourceSettings(statement_timeout_ms=1500, fetch_rows=2))
    rows = await collect(connector, "2026-01-01")
    assert rows[0] == ("purchase_orders:1", ROWS[0])
    assert [locator for locator, _ in rows] == [f"purchase_orders:{n}" for n in range(1, 6)]
    assert connector.columns == ("po_number", "total")
    log = mysql_engine["log"]
    assert log[:4] == ["SET SESSION TRANSACTION READ ONLY", limit, "ROLLBACK", "BEGIN"]
    assert log[4][1] == {"since": "2026-01-01"}
    assert log[-1] == "END"
    url, options = mysql_engine["options"][0]
    assert url.drivername == "mysql+asyncmy"
    assert options["connect_args"] == {
        "connect_timeout": 10.0,
        "program_name": "lorebase-scribe",
        "charset": "utf8mb4",
    }
    assert mysql_engine["engine"].disposed


async def test_mysql_keeps_the_charset_of_the_url(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch, mysql_engine: dict[str, Any]
) -> None:
    source = erp_source(
        supply_schema,
        monkeypatch,
        "mariadb://u:p@db/erp?charset=latin1",
        type=SourceType.MYSQL_TABLE,
    )
    await collect(MysqlConnector(source, MysqlSourceSettings()))
    assert "charset" not in mysql_engine["options"][0][1]["connect_args"]


async def test_mssql_rows_are_fetched_in_threads_and_closed(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch, mssql_engine: dict[str, Any]
) -> None:
    source = erp_source(
        supply_schema, monkeypatch, "mssql://u:p@db:1433/erp", type=SourceType.MSSQL_TABLE
    )
    connector = MssqlConnector(source, MssqlSourceSettings(statement_timeout_ms=2500, fetch_rows=2))
    rows = await collect(connector)
    assert [row for _, row in rows] == ROWS
    assert connector.columns == ("po_number", "total")
    assert mssql_engine["log"][0][0] == "SET LOCK_TIMEOUT 2500"
    url, options = mssql_engine["options"][0]
    assert url.drivername == "mssql+pymssql"
    assert options["connect_args"] == {
        "login_timeout": 10,
        "timeout": 3,
        "appname": "lorebase-scribe",
        "use_datetime2": True,
    }
    engine = mssql_engine["engine"]
    assert engine.disposed
    assert engine.connection is not None
    assert engine.connection.closed


async def test_mssql_closes_when_the_consumer_stops_early(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch, mssql_engine: dict[str, Any]
) -> None:
    source = erp_source(
        supply_schema, monkeypatch, "mssql://u:p@db/erp", type=SourceType.MSSQL_TABLE
    )
    rows = MssqlConnector(source, MssqlSourceSettings(fetch_rows=1)).rows(None)
    assert (await anext(rows))[0] == "purchase_orders:1"
    await rows.aclose()  # type: ignore[attr-defined]
    assert mssql_engine["engine"].disposed


async def test_driver_errors_become_read_errors_without_the_connection(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch, mysql_engine: dict[str, Any]
) -> None:
    class Broken(FakeAsyncEngine):
        @asynccontextmanager
        async def connect(self) -> AsyncIterator[FakeAsyncConnection]:
            raise OperationalError("SELECT 1", {}, Exception("Access denied for user 'u'"))
            yield  # pragma: no cover

    monkeypatch.setattr(
        MysqlConnector,
        "engine_factory",
        staticmethod(lambda url, **options: Broken([], [], False)),
    )
    source = erp_source(
        supply_schema, monkeypatch, "mysql://u:topsecret@db/erp", type=SourceType.MYSQL_TABLE
    )
    with pytest.raises(SourceReadError, match="Access denied") as caught:
        await collect(MysqlConnector(source, MysqlSourceSettings()))
    assert "topsecret" not in str(caught.value)


async def test_a_sql_source_is_profiled_for_inference(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch, mssql_engine: dict[str, Any]
) -> None:
    source = erp_source(
        supply_schema,
        monkeypatch,
        "mssql://u:p@db/erp",
        type=SourceType.MSSQL_TABLE,
        scope_filter=None,
    )
    settings = SourceSettings(allowed_connections=(CONNECTION,))
    profile = await profile_source(source, settings, RecordProfilingSettings())
    assert profile.name == "purchase_orders"
    assert profile.row_count == 5
    assert [column.name for column in profile.columns] == ["po_number", "total"]


@pytest.mark.parametrize(
    ("query", "ordered"),
    [
        ("SELECT a FROM t ORDER BY a", True),
        ("select a from t order by a desc, b", True),
        ("SELECT a FROM t UNION SELECT a FROM u ORDER BY a", True),
        ("SELECT a FROM t ORDER BY a -- newest first", True),
        ("SELECT TOP 5 a FROM t ORDER BY a", False),
        ("SELECT a FROM t ORDER BY a OFFSET 5 ROWS", False),
        ("SELECT a FROM t", False),
        ("SELECT a FROM (SELECT a FROM t ORDER BY a OFFSET 0 ROWS) x", False),
        ("SELECT 'order by' AS [order by] FROM t -- ORDER BY x", False),
        ("SELECT a FROM t /* ORDER BY a */", False),
    ],
)
def test_mssql_order_by_gets_offset(query: str, ordered: bool) -> None:
    assert mssql_orderable(query) == (query + "\nOFFSET 0 ROWS" if ordered else query)


def test_mssql_query_statement_with_order_by(supply_schema: Schema) -> None:
    base = supply_schema.source_by_api_name("erp.purchase_orders")
    assert base is not None
    source = base.model_copy(
        update={
            "type": SourceType.MSSQL_QUERY,
            "query": "SELECT po_number FROM dbo.po ORDER BY po_number;",
            "scope_filter": None,
            "watermark_column": None,
            "sync_mode": SyncMode.SNAPSHOT,
        }
    )
    sql, _, _ = MssqlConnector(source, MssqlSourceSettings()).statement(None)
    assert sql == (
        "SELECT * FROM (SELECT po_number FROM dbo.po ORDER BY po_number\nOFFSET 0 ROWS)"
        " AS lorebase_source"
    )
    mysql = source.model_copy(update={"type": SourceType.MYSQL_QUERY})
    sql, _, _ = MysqlConnector(mysql, MysqlSourceSettings()).statement(None)
    assert "OFFSET" not in sql


def test_rowversion_watermarks_round_trip_as_hex_bytes(supply_schema: Schema) -> None:
    stored = encode_watermark(bytes.fromhex("00000000000007d1"))
    assert stored == {"type": "bytes", "value": "00000000000007d1"}
    since = decode_watermark(stored)
    assert since == b"\x00\x00\x00\x00\x00\x00\x07\xd1"
    base = supply_schema.source_by_api_name("erp.purchase_orders")
    assert base is not None
    source = base.model_copy(
        update={"type": SourceType.MSSQL_TABLE, "table": "dbo.po", "watermark_column": "rv"}
    )
    sql, parameters, _ = MssqlConnector(source, MssqlSourceSettings()).statement(since)
    assert parameters == {"since": since}
    assert isinstance(parameters["since"], bytes)
    assert "[rv] > :since ORDER BY [rv]" in sql
