"""MySQL, MariaDB and SQL Server profiler of schema inference, through SQLite stand-ins."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, event, text

from lorebase_core.infer import TableProfile, infer_schema
from lorebase_store.profiling import SqlDriverError, SqlProfileError, profile_sql_database
from lorebase_store.profiling import sql_databases as module
from lorebase_store.settings import RecordProfilingSettings, SqlProfilingSettings

RECORDS = RecordProfilingSettings()


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    found = create_engine(f"sqlite:///{tmp_path / 'erp.db'}")
    with found.begin() as connection:
        connection.execute(text("CREATE TABLE supplier (code TEXT PRIMARY KEY, name TEXT)"))
        connection.execute(
            text(
                "CREATE TABLE purchase_order (po TEXT PRIMARY KEY, supplier_code TEXT, qty INTEGER,"
                " rv BLOB, FOREIGN KEY (supplier_code) REFERENCES supplier (code))"
            )
        )
        for number in range(1, 6):
            connection.execute(
                text("INSERT INTO supplier VALUES (:c, :n)"),
                {"c": f"S-{number}", "n": f"Supplier {number}"},
            )
        for number in range(1, 9):
            connection.execute(
                text("INSERT INTO purchase_order VALUES (:p, :s, :q, :r)"),
                {"p": f"PO-{number}", "s": f"S-{number % 5 + 1}", "q": number, "r": b"\x00\x01"},
            )
    yield found
    found.dispose()


def by_name(profiles: list[TableProfile]) -> dict[str, TableProfile]:
    return {profile.name: profile for profile in profiles}


def values(profile: TableProfile, column: str) -> list[Any]:
    return list(next(c for c in profile.columns if c.name == column).values)


def sql_server(engine: Engine, seen: list[Any]) -> Any:
    """An engine factory handing out `engine`; the T-SQL session statements become no-ops."""

    def factory(url: Any, **options: Any) -> Engine:
        seen.append((url, options))
        fresh = create_engine(engine.url)  # the code under test disposes what it is handed

        @event.listens_for(fresh, "before_cursor_execute", retval=True)
        def rewrite(_c: Any, _k: Any, statement: str, parameters: Any, *_: Any) -> Any:
            seen.append(statement)
            return ("SELECT 1", ()) if statement.startswith("SET ") else (statement, parameters)

        return fresh

    return factory


async def test_sql_server_tables_are_sampled_and_described(engine: Engine) -> None:
    seen: list[Any] = []
    settings = SqlProfilingSettings(sample_rows=3, statement_timeout_ms=2500, connect_timeout_s=4)
    profiles = by_name(
        await profile_sql_database(
            "mssql",
            "sqlserver://u:p@db:1433/erp",
            settings,
            RECORDS,
            engine_factory=sql_server(engine, seen),
        )
    )
    assert set(profiles) == {"purchase_order", "supplier"}
    orders = profiles["purchase_order"]
    assert len(values(orders, "po")) == 3
    assert orders.row_count == 8  # counted, so inference knows the sample is partial
    assert orders.primary_key == ("po",)
    assert [(fk.columns, fk.target_table) for fk in orders.foreign_keys] == [
        (("supplier_code",), "supplier")
    ]
    assert values(orders, "rv") == ["0001"] * 3  # binary values (rowversion) as hex text
    assert profiles["supplier"].row_count == 5
    url, options = seen[0]
    assert url.drivername == "mssql+pymssql"
    assert options["connect_args"] == {
        "login_timeout": 4,
        "timeout": 3,
        "appname": "lorebase-keeper",
    }
    assert any(isinstance(item, str) and item == "SET LOCK_TIMEOUT 2500" for item in seen)


async def test_named_tables_only_and_unknown_tables_are_refused(engine: Engine) -> None:
    factory = sql_server(engine, [])
    only = await profile_sql_database(
        "mssql", "mssql://u:p@h/db", SqlProfilingSettings(), RECORDS,
        tables=["supplier"], engine_factory=factory,
    )  # fmt: skip
    assert [profile.name for profile in only] == ["supplier"]
    with pytest.raises(SqlProfileError, match=r"unknown tables.*missing"):
        await profile_sql_database(
            "mssql", "mssql://u:p@h/db", SqlProfilingSettings(), RECORDS,
            tables=["missing"], engine_factory=factory,
        )  # fmt: skip
    with pytest.raises(SqlProfileError, match="more than the 1 allowed"):
        await profile_sql_database(
            "mssql", "mssql://u:p@h/db", SqlProfilingSettings(max_tables=1), RECORDS,
            engine_factory=factory,
        )  # fmt: skip


async def test_the_count_can_be_skipped(engine: Engine) -> None:
    (profile,) = await profile_sql_database(
        "mssql", "mssql://u:p@h/db", SqlProfilingSettings(sample_rows=2, count_rows=False),
        RECORDS, tables=["supplier"], engine_factory=sql_server(engine, []),
    )  # fmt: skip
    assert profile.row_count == 2


async def test_profiles_feed_inference(engine: Engine) -> None:
    profiles = await profile_sql_database(
        "mssql", "mssql://u:p@h/db", SqlProfilingSettings(), RECORDS,
        engine_factory=sql_server(engine, []),
    )  # fmt: skip
    proposal = infer_schema(profiles, "supply")
    assert {kind.api_name for kind in proposal.schema.kinds.values()} == {
        "PurchaseOrder",
        "Supplier",
    }
    assert [relation.api_name for relation in proposal.schema.relations.values()] == ["supplier"]


class FakeAsyncConnection:
    def __init__(self, sync: Any, is_mariadb: bool, log: list[str]) -> None:
        self._sync = sync
        self.dialect = SimpleNamespace(is_mariadb=is_mariadb)
        self._log = log

    async def execute(self, statement: Any) -> None:
        self._log.append(str(statement))

    async def rollback(self) -> None:
        self._log.append("ROLLBACK")

    async def run_sync(self, function: Any) -> Any:
        return function(self._sync)


class FakeAsyncEngine:
    def __init__(self, sync: Engine, is_mariadb: bool, log: list[str]) -> None:
        self.sync, self.is_mariadb, self.log, self.disposed = sync, is_mariadb, log, False

    @asynccontextmanager
    async def connect(self) -> Any:
        with self.sync.connect() as connection:
            yield FakeAsyncConnection(connection, self.is_mariadb, self.log)

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.parametrize(
    ("mariadb", "limit"),
    [
        (False, "SET SESSION max_execution_time = 2500"),
        (True, "SET SESSION max_statement_time = 3"),
    ],
)
async def test_mysql_sessions_are_read_only_and_bounded(
    engine: Engine, mariadb: bool, limit: str
) -> None:
    log: list[str] = []
    options: dict[str, Any] = {}
    made: list[FakeAsyncEngine] = []

    def factory(url: Any, **kwargs: Any) -> FakeAsyncEngine:
        options.update(drivername=url.drivername, **kwargs)
        made.append(FakeAsyncEngine(engine, mariadb, log))
        return made[0]

    profiles = await profile_sql_database(
        "mysql",
        "mariadb://u:p@db/erp",
        SqlProfilingSettings(statement_timeout_ms=2500),
        RECORDS,
        tables=["supplier"],
        engine_factory=factory,
    )
    assert [profile.name for profile in profiles] == ["supplier"]
    assert log == ["SET SESSION TRANSACTION READ ONLY", limit, "ROLLBACK"]
    assert options["drivername"] == "mysql+asyncmy"
    assert options["connect_args"]["charset"] == "utf8mb4"
    assert made[0].disposed


async def test_connections_of_another_database_are_refused_without_echo() -> None:
    with pytest.raises(SqlProfileError, match="must be a mssql:// URL") as refused:
        await profile_sql_database(
            "mssql", "postgresql://u:hunter2@h/db", SqlProfilingSettings(), RECORDS
        )
    assert "hunter2" not in str(refused.value)
    with pytest.raises(SqlProfileError, match="not a valid database URL") as garbled:
        await profile_sql_database("mysql", "not a url hunter2", SqlProfilingSettings(), RECORDS)
    assert "hunter2" not in str(garbled.value)


async def test_a_missing_driver_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "pymssql", None)
    with pytest.raises(SqlDriverError, match=r"install lorebase-keeper\[mssql\]"):
        await profile_sql_database("mssql", "mssql://u:p@h/db", SqlProfilingSettings(), RECORDS)
    assert module.APPLICATION_NAME == "lorebase-keeper"
