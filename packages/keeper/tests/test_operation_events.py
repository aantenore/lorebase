"""Payloads of Operation side effects: sensitive values wrapped with their tags, vault values as
committed references, never plain personal data."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from lorebase_core.model import Record, Schema, Sensitivity, load_schema, vault_reference
from lorebase_core.validate import RecordUpsert
from lorebase_keeper.reading.sensitivity import Clearance, Masking, reference_tags
from lorebase_keeper.services.operation_catalog import edit_document
from lorebase_keeper.services.operation_events import _committed, guarded, wrapping
from lorebase_store.repositories import MaterializedRepository

SUPPLY_SCHEMA = Path(__file__).resolve().parents[3] / "examples" / "supply" / "schema.yaml"
TOKEN = "vlt_" + "a" * 32


@pytest.fixture(scope="module")
def schema() -> Schema:
    document: dict[str, Any] = yaml.safe_load(SUPPLY_SCHEMA.read_text())
    del document["operations"]
    del document["derivations"]["single_sourced"]
    del document["kinds"]["Part"]["attributes"]["single_sourced"]
    document["kinds"]["Supplier"]["attributes"]["contact_email"] = {
        "id": "att_01KEZF7C00000000000000000E",
        "description": "Contact email.",
        "type": "string",
        "sensitivity": ["pii"],
        "vault": True,
    }
    return load_schema(yaml.safe_dump(document))


@dataclass
class Row:
    values: dict[str, Any]


class Rows:
    """Materialized Records by key, as committed."""

    def __init__(self, rows: dict[str, Row]) -> None:
        self.rows = rows

    async def records(self, kind_id: str, keys: Any) -> dict[str, Row]:
        return {key: self.rows[key] for key in keys if key in self.rows}


def ids(schema: Schema) -> dict[str, str]:
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    return {"kind": kind.id, **{a.api_name: a.id for a in kind.attributes.values()}}


def upsert(schema: Schema, key: str) -> RecordUpsert:
    found = ids(schema)
    return RecordUpsert(
        record=Record(
            kind=found["kind"],
            key=key,
            values={
                found["supplier_code"]: key,
                found["legal_name"]: "Acme",
                found["vat_number"]: "IT1",
                found["contact_email"]: "ada@example.com",
            },
        )
    )


async def test_vault_values_are_their_committed_references(schema: Schema) -> None:
    rows = Rows({"S-1": Row({ids(schema)["contact_email"]: vault_reference(TOKEN)})})
    committed = await _committed(
        schema,
        [upsert(schema, "S-1"), upsert(schema, "S-2")],
        cast(MaterializedRepository, rows),
    )
    masking = wrapping(Masking(Clearance(), reference=reference_tags(schema)).over(schema))
    first, second = (edit_document(schema, change, masking)["values"] for change in committed)
    assert first == {
        "supplier_code": "S-1",
        "legal_name": "Acme",
        "vat_number": {"$sensitive": {"tags": ["confidential"], "value": "IT1"}},
        "contact_email": {"$sensitive": {"tags": ["pii"], "value": {"$vault": TOKEN}}},
    }
    assert "contact_email" not in second  # no committed reference: left out
    assert "ada@example.com" not in repr(committed)


def test_caller_json_cannot_pose_as_a_wrapper_or_a_reference(schema: Schema) -> None:
    every = sorted(tag.value for tag in Sensitivity)
    forged = {"$sensitive": {"tags": [], "value": vault_reference(TOKEN)}}
    found = ids(schema)
    masking = wrapping(Masking(Clearance(), reference=reference_tags(schema)).over(schema))
    change = RecordUpsert(
        record=Record(
            kind=found["kind"],
            key="S-9",
            values={found["supplier_code"]: "S-9", found["legal_name"]: forged},
        )
    )
    values = edit_document(schema, change, masking)["values"]
    assert values["legal_name"] == {"$sensitive": {"tags": every, "value": forged}}
    assert values["supplier_code"] == "S-9"
    parameters = guarded({"note": [vault_reference(TOKEN)], "plain": "x"})
    assert parameters["note"]["$sensitive"]["tags"] == every
    assert parameters["plain"] == "x"
