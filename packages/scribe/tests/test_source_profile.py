"""Profiles of Sources through their connectors, and the column check of rows without a header."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lorebase_core.infer import infer_schema
from lorebase_core.model import Schema, Source
from lorebase_core.model.source import SourceType
from lorebase_core.types import AttributeType
from lorebase_scribe.settings import JsonSourceSettings, SourceSettings
from lorebase_scribe.sources import profile_source
from lorebase_scribe.sources.mapping import Row, RowMapper, SourceDefinitionError
from lorebase_scribe.sources.sync import SourceSync, SyncReport
from lorebase_store.settings import RecordProfilingSettings

CONNECTION = "secret://env/TEST_RECORDS_LOCATION"


def records_source(schema: Schema, monkeypatch: pytest.MonkeyPatch, path: Path) -> Source:
    monkeypatch.setenv("TEST_RECORDS_LOCATION", str(path))
    found = schema.source_by_api_name("approved.suppliers")
    assert found is not None
    return found.model_copy(
        update={"type": SourceType.JSONL, "connection": CONNECTION, "flatten_separator": "_"}
    )


async def test_a_json_lines_source_is_profiled_for_inference(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "suppliers.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "supplier_code": f"S-{n:04d}",
                    "risk_score": n % 100,
                    "address": {"city": "Turin"},
                    **({"note": "late"} if n == 7 else {}),
                }
            )
            for n in range(50)
        )
    )
    source = records_source(supply_schema, monkeypatch, path)
    settings = SourceSettings(
        allowed_connections=(CONNECTION,),
        json_files=JsonSourceSettings(allowed_roots=(str(tmp_path),)),
    )
    profile = await profile_source(source, settings, RecordProfilingSettings(sample_rows=20))
    assert profile.name == "suppliers"
    assert profile.row_count == 50
    assert [column.name for column in profile.columns] == [
        "supplier_code",
        "risk_score",
        "address_city",
        "note",
    ]
    assert len(list(profile.columns[0].values)) == 20
    proposal = infer_schema([profile], "supply")
    kind = next(iter(proposal.schema.kinds.values()))
    types = {attribute.api_name: attribute.type.name for attribute in kind.attributes.values()}
    assert types["risk_score"] is AttributeType.INTEGER


async def test_too_many_columns_are_a_definition_error(
    tmp_path: Path, supply_schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "wide.jsonl"
    path.write_text(json.dumps({f"c{n}": n for n in range(5)}))
    source = records_source(supply_schema, monkeypatch, path)
    settings = SourceSettings(
        allowed_connections=(CONNECTION,),
        json_files=JsonSourceSettings(allowed_roots=(str(tmp_path),)),
    )
    with pytest.raises(SourceDefinitionError, match="more than the 3 columns"):
        await profile_source(source, settings, RecordProfilingSettings(max_columns=3))


# The column check of Sources without fixed columns ----------------------------------------------


@dataclass
class Records:
    """A connector yielding fixed records, without a header."""

    records: list[dict[str, Any]]

    @property
    def columns(self) -> tuple[str, ...]:
        return ()

    async def rows(self, since: Any) -> AsyncIterator[tuple[str, Row]]:
        for number, record in enumerate(self.records, start=1):
            yield f"records:{number}", record


async def read(schema: Schema, records: list[dict[str, Any]]) -> SyncReport:
    source = schema.source_by_api_name("approved.suppliers")
    assert source is not None
    settings = SourceSettings(max_row_errors=1)
    sync = SourceSync(
        None,  # type: ignore[arg-type]  # reading needs no database
        None,  # type: ignore[arg-type]
        settings,
        principal="svc_scribe",
        connectors=lambda source, settings: Records(records),
    )
    report = SyncReport("srn_1", "supply", source.id, source.api_name, "b", "main", True)
    await sync._read(RowMapper(schema, source), source, None, report)
    return report


async def test_keys_missing_from_some_records_read_as_null(supply_schema: Schema) -> None:
    source = supply_schema.source_by_api_name("approved.suppliers")
    assert source is not None
    columns = sorted(RowMapper(supply_schema, source).columns)
    full = dict.fromkeys(columns, None) | {"supplier_code": "S-1", "legal_name": "Acme"}
    sparse = {"supplier_code": "S-2", "legal_name": "Beta"}
    report = await read(supply_schema, [sparse, full])
    assert report.rows_read == 2
    assert report.rows_failed == 0


async def test_a_mapped_column_no_record_has_is_refused(supply_schema: Schema) -> None:
    with pytest.raises(SourceDefinitionError, match="columns missing from the source"):
        await read(supply_schema, [{"supplier_code": "S-1"}])
    # Failures caused by a missing key column report the missing column, not the failures.
    with pytest.raises(SourceDefinitionError, match="supplier_code"):
        await read(supply_schema, [{"code": "S-1"}, {"code": "S-2"}, {"code": "S-3"}])
