"""CSV and Postgres connectors without a network or a database."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import AsyncIterator
from pathlib import Path, PurePath
from typing import Any

import httpx
import pytest

from lorebase_core.model import Schema, Source
from lorebase_core.model.source import SourceType, SyncMode
from lorebase_scribe.settings import CsvSourceSettings, PostgresSourceSettings
from lorebase_scribe.sources import access
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


def transport(status: int = 200, body: bytes = CSV, seen: list[httpx.Request] | None = None) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        headers = {"location": "https://elsewhere.test/x.csv"} if status == 302 else {}
        return httpx.Response(status, content=body, headers=headers)

    return httpx.MockTransport(handle)


PUBLIC = "93.184.215.14"


def resolver(*addresses: str) -> Any:
    """A host lookup answering `addresses` for every host name."""

    async def resolve(host: str) -> list[str]:
        return list(addresses)

    return resolve


async def test_download_from_an_allowed_host(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = csv_source(supply_schema, monkeypatch, "https://files.example.test/suppliers.csv")
    settings = CsvSourceSettings(allowed_hosts=("files.example.test",))
    seen: list[httpx.Request] = []
    connector = CsvConnector(source, settings, transport(seen=seen), resolver(PUBLIC))
    rows = await collect(connector)
    assert [locator for locator, _ in rows] == ["suppliers.csv:2", "suppliers.csv:4"]
    # The connection goes to the checked address; the host name stays for HTTP and TLS.
    assert [str(request.url) for request in seen] == [f"https://{PUBLIC}/suppliers.csv"]
    assert seen[0].headers["Host"] == "files.example.test"
    assert seen[0].extensions["sni_hostname"] == "files.example.test"


async def test_download_refusals(supply_schema: Schema, monkeypatch: pytest.MonkeyPatch) -> None:
    allowed = CsvSourceSettings(allowed_hosts=("files.example.test",))
    public = resolver(PUBLIC)
    other = csv_source(supply_schema, monkeypatch, "https://other.test/suppliers.csv")
    with pytest.raises(SourceDefinitionError, match="allowed_hosts"):
        await collect(CsvConnector(other, allowed, transport(), public))
    plain = csv_source(supply_schema, monkeypatch, "http://files.example.test/suppliers.csv")
    with pytest.raises(SourceDefinitionError, match="allowed_schemes"):
        await collect(CsvConnector(plain, allowed, transport(), public))
    good = csv_source(supply_schema, monkeypatch, "https://files.example.test/suppliers.csv")
    with pytest.raises(SourceReadError, match="302"):
        await collect(CsvConnector(good, allowed, transport(status=302), public))
    tiny = CsvSourceSettings(allowed_hosts=("files.example.test",), max_bytes=10)
    with pytest.raises(SourceDefinitionError, match="larger"):
        await collect(CsvConnector(good, tiny, transport(), public))


@pytest.mark.parametrize(
    "addresses", [("10.0.0.5",), (PUBLIC, "169.254.169.254"), ("100.64.0.1",), ("::1",)]
)
async def test_url_hosts_resolving_to_private_addresses_are_refused(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch, addresses: tuple[str, ...]
) -> None:
    source = csv_source(supply_schema, monkeypatch, "https://files.example.test/suppliers.csv")
    settings = CsvSourceSettings(allowed_hosts=("files.example.test",))
    seen: list[httpx.Request] = []
    connector = CsvConnector(source, settings, transport(seen=seen), resolver(*addresses))
    with pytest.raises(SourceDefinitionError, match="allow_private_targets") as caught:
        await collect(connector)
    assert seen == []
    assert "files.example.test" not in str(caught.value)
    assert not any(address in str(caught.value) for address in addresses)


async def test_private_url_hosts_can_be_allowed(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = csv_source(supply_schema, monkeypatch, "https://files.internal.test/suppliers.csv")
    settings = CsvSourceSettings(allowed_hosts=("files.internal.test",), allow_private_targets=True)
    seen: list[httpx.Request] = []
    connector = CsvConnector(source, settings, transport(seen=seen), resolver("10.0.0.5"))
    assert len(await collect(connector)) == 2
    assert [str(request.url) for request in seen] == ["https://files.internal.test/suppliers.csv"]


async def test_url_hosts_that_cannot_be_resolved_fail_the_read(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unknown(host: str) -> list[str]:
        raise OSError(f"no such host {host}")

    source = csv_source(supply_schema, monkeypatch, "https://files.example.test/suppliers.csv")
    settings = CsvSourceSettings(allowed_hosts=("files.example.test",))
    with pytest.raises(SourceReadError, match="cannot be resolved") as caught:
        await collect(CsvConnector(source, settings, transport(), unknown))
    assert "files.example.test" not in str(caught.value)


class EndlessBody(httpx.AsyncByteStream):
    """A response body without a length that never ends."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            yield b"x" * 4096


async def test_an_endless_download_stops_at_max_bytes(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = csv_source(supply_schema, monkeypatch, "https://files.example.test/suppliers.csv")
    settings = CsvSourceSettings(allowed_hosts=("files.example.test",), max_bytes=100_000)
    endless = httpx.MockTransport(lambda request: httpx.Response(200, stream=EndlessBody()))
    with pytest.raises(SourceDefinitionError, match="max_bytes"):
        await collect(CsvConnector(source, settings, endless, resolver(PUBLIC)))


# Local files: symbolic links, file types and sizes -------------------------------------------


def local(tmp_path: Path) -> tuple[Path, Path, CsvSourceSettings]:
    """An allowed root holding `suppliers.csv`, and a directory outside it."""
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "suppliers.csv").write_bytes(CSV)
    (outside / "secret.csv").write_bytes(b"secret;value\nS-9;hidden\n")
    return root, outside, CsvSourceSettings(allowed_roots=(str(root),))


async def test_a_link_inside_a_root_pointing_outside_is_refused(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, outside, settings = local(tmp_path)
    (root / "link.csv").symlink_to(outside / "secret.csv")
    (root / "dir").symlink_to(outside, target_is_directory=True)
    for location in (root / "link.csv", root / "dir" / "secret.csv"):
        source = csv_source(supply_schema, monkeypatch, str(location))
        with pytest.raises(SourceDefinitionError, match="allowed_roots"):
            await collect(CsvConnector(source, settings))


async def test_a_link_inside_a_root_pointing_inside_it_is_read(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Links are resolved before the roots check, so links within a root keep working.
    root, _, settings = local(tmp_path)
    (root / "current.csv").symlink_to(root / "suppliers.csv")
    source = csv_source(supply_schema, monkeypatch, str(root / "current.csv"))
    assert len(await collect(CsvConnector(source, settings))) == 2


@pytest.mark.parametrize("swapped", ["suppliers.csv", "sub"])
async def test_a_link_swapped_in_after_the_check_is_not_followed(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch, swapped: str
) -> None:
    root, outside, settings = local(tmp_path)
    (root / "sub").mkdir()
    (root / "sub" / "secret.csv").write_bytes(CSV)
    location = root / swapped if swapped == "suppliers.csv" else root / swapped / "secret.csv"
    check_then_open = access._open_beneath

    def swap_then_open(base: Path, relative: PurePath) -> int:
        # A writer inside the root replaces a checked component with a link to elsewhere.
        replaced = root / swapped
        if replaced.is_dir():
            shutil.rmtree(replaced)
            replaced.symlink_to(outside, target_is_directory=True)
        else:
            replaced.unlink()
            replaced.symlink_to(outside / "secret.csv")
        return check_then_open(base, relative)

    monkeypatch.setattr(access, "_open_beneath", swap_then_open)
    source = csv_source(supply_schema, monkeypatch, str(location))
    with pytest.raises(SourceDefinitionError, match="symbolic link"):
        await collect(CsvConnector(source, settings))


async def test_files_that_are_not_regular_are_refused(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, settings = local(tmp_path)
    (root / "folder.csv").mkdir()
    os.mkfifo(root / "pipe.csv")  # opening must not wait for a writer
    for name in ("folder.csv", "pipe.csv"):
        source = csv_source(supply_schema, monkeypatch, str(root / name))
        with pytest.raises(SourceDefinitionError, match="not a regular file"):
            await collect(CsvConnector(source, settings))
    source = csv_source(supply_schema, monkeypatch, str(root))
    with pytest.raises(SourceDefinitionError, match="allowed_roots"):
        await collect(CsvConnector(source, settings))


async def test_a_file_growing_after_fstat_stops_at_max_bytes(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = local(tmp_path)
    settings = CsvSourceSettings(allowed_roots=(str(root),), max_bytes=len(CSV) - 1)
    real_fstat = os.fstat

    def small_fstat(descriptor: int) -> os.stat_result:
        status = real_fstat(descriptor)
        values = list(status)
        values[stat.ST_SIZE] = 1  # the size when checked; the file grows afterwards
        return os.stat_result(values)

    monkeypatch.setattr(access.os, "fstat", small_fstat)
    source = csv_source(supply_schema, monkeypatch, str(root / "suppliers.csv"))
    with pytest.raises(SourceDefinitionError, match=r"scribe\.sources\.csv\.max_bytes"):
        await collect(CsvConnector(source, settings))


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
