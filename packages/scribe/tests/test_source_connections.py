"""Which secret references a Source may resolve, and what its errors may say about them."""

from __future__ import annotations

from pathlib import Path

import pytest

from lorebase_core.model import Schema, Source
from lorebase_scribe.settings import CsvSourceSettings, SourceSettings
from lorebase_scribe.sources.connectors import CsvConnector, default_connector
from lorebase_scribe.sources.mapping import SourceDefinitionError

SECRET_VALUE = "not-a-path-but-a-secret-0123456789abcdef"


def source(schema: Schema, api_name: str, connection: str) -> Source:
    found = schema.source_by_api_name(api_name)
    assert found is not None
    return found.model_copy(update={"connection": connection})


@pytest.mark.parametrize("api_name", ["approved.suppliers", "erp.purchase_orders"])
def test_a_connection_outside_the_allowlist_is_refused(
    supply_schema: Schema, api_name: str
) -> None:
    # A schema author could otherwise name any reference the Scribe host can resolve,
    # such as the DSN of the Lorebase database itself.
    found = source(supply_schema, api_name, "secret://env/LOREBASE_DB_DSN")
    with pytest.raises(SourceDefinitionError, match="allowed_connections"):
        default_connector(found, SourceSettings())
    other = SourceSettings(allowed_connections=("secret://env/ERP_DSN",))
    with pytest.raises(SourceDefinitionError, match="allowed_connections"):
        default_connector(found, other)


def test_an_allowed_connection_gets_its_connector(supply_schema: Schema) -> None:
    found = source(supply_schema, "erp.purchase_orders", "secret://env/ERP_DSN")
    settings = SourceSettings(allowed_connections=("secret://env/ERP_DSN",))
    assert default_connector(found, settings) is not None


async def test_errors_never_echo_the_resolved_location(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOME_SECRET", SECRET_VALUE)
    found = source(supply_schema, "approved.suppliers", "secret://env/SOME_SECRET")
    connector = CsvConnector(found, CsvSourceSettings(allowed_roots=(str(tmp_path),)))
    with pytest.raises(SourceDefinitionError) as caught:
        async for _ in connector.rows(None):
            pass
    assert SECRET_VALUE not in str(caught.value)
    assert "allowed_roots" in str(caught.value)


async def test_url_errors_never_echo_the_resolved_host(
    supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOME_SECRET", f"https://{SECRET_VALUE}.test/x.csv")
    found = source(supply_schema, "approved.suppliers", "secret://env/SOME_SECRET")
    connector = CsvConnector(found, CsvSourceSettings(allowed_hosts=("files.example.test",)))
    with pytest.raises(SourceDefinitionError) as caught:
        async for _ in connector.rows(None):
            pass
    assert SECRET_VALUE not in str(caught.value)
    assert "allowed_hosts" in str(caught.value)
