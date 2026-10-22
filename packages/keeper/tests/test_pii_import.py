"""What needs `pii.import`: import rows holding vault values, Sources feeding vault Attributes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from lorebase_core.model import Schema, load_schema
from lorebase_keeper.services.operation_catalog import (
    IMPORT_BATCH,
    IMPORT_LINKS,
    IMPORT_RECORDS,
    builtin_definition,
)
from lorebase_keeper.services.pii_import import (
    fed_vault_sources,
    import_rows_vault_attributes,
    source_vault_targets,
)

SUPPLY_SCHEMA = Path(__file__).resolve().parents[3] / "examples" / "supply" / "schema.yaml"
CONTACTS = "contacts.suppliers"


def _document(*, source: dict[str, Any] | None = None) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(SUPPLY_SCHEMA.read_text())
    document["kinds"]["Supplier"]["attributes"]["email"] = {
        "id": "att_01KEZF7C00VAVTEMA000000001",
        "description": "Contact email.",
        "type": "string",
        "sensitivity": ["pii"],
        "vault": True,
    }
    if source is not None:
        document["sources"][CONTACTS] = {
            "id": "src_01KEZF7C00VAVTEMA000000002",
            "description": "Supplier contacts.",
            "type": "csv",
            "connection": "secret://env/SUPPLY_CONTACTS_CSV",
            "target_kind": "Supplier",
            "key_mapping": ["supplier_code"],
            **source,
        }
    return document


def _schema(**options: Any) -> Schema:
    return load_schema(yaml.safe_dump(_document(**options)))


@pytest.fixture(scope="module")
def schema() -> Schema:
    return _schema()


def _vaulted(schema: Schema, name: str, parameters: dict[str, Any]) -> list[str]:
    definition = builtin_definition(schema, name)
    assert definition is not None
    return import_rows_vault_attributes(schema, definition, parameters)


def test_import_rows_holding_vault_values_are_found(schema: Schema) -> None:
    row = {"supplier_code": "S-1", "legal_name": "A", "country": "DE"}
    records = {"kind": "Supplier", "rows": [row, {**row, "email": "a@example.com"}]}
    assert _vaulted(schema, IMPORT_RECORDS, records) == ["email"]
    assert _vaulted(schema, IMPORT_RECORDS, {"kind": "Supplier", "rows": [row]}) == []
    nulled = {"kind": "Supplier", "rows": [{**row, "email": None}]}
    assert _vaulted(schema, IMPORT_RECORDS, nulled) == []
    batch = {"records": [{"kind": "Supplier", **row, "email": "a@example.com"}]}
    assert _vaulted(schema, IMPORT_BATCH, batch) == ["email"]
    # Malformed parameters are left to binding.
    assert _vaulted(schema, IMPORT_RECORDS, {"kind": "Supplier", "rows": "nope"}) == []
    assert _vaulted(schema, IMPORT_BATCH, {"records": [1, {"email": "x"}]}) == []
    assert _vaulted(schema, IMPORT_LINKS, {"relation": "x", "rows": [{"email": "x"}]}) == []


def test_sources_feeding_vault_attributes_are_found(schema: Schema) -> None:
    feeding = _schema(source={"attribute_mapping": {"email": {"column": "email"}}})
    plain = _schema(source={"attribute_mapping": {"legal_name": {"column": "name"}}})
    source = feeding.source_by_api_name(CONTACTS)
    assert source is not None
    assert source_vault_targets(feeding, source) == {"att_01KEZF7C00VAVTEMA000000001"}
    # Added, changed, or newly mapping a vault Attribute: fed.
    assert fed_vault_sources(schema, feeding) == [source.id]
    assert fed_vault_sources(plain, feeding) == [source.id]
    renamed = _schema(
        source={"attribute_mapping": {"email": {"column": "mail"}}},
    )
    assert fed_vault_sources(feeding, renamed) == [source.id]
    # Unchanged, removed, or not mapping vault Attributes: not fed.
    assert fed_vault_sources(feeding, feeding) == []
    assert fed_vault_sources(feeding, schema) == []
    assert fed_vault_sources(schema, plain) == []
