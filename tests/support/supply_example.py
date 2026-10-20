"""The `supply` example as the integration tests of the client and the CLI use it.

Expressions are not enabled in Keeper yet, so the schema loaded into it is the example without
the Operations and the Derivation written in CEL (and the Attribute the Derivation fills).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "supply"
DATA = EXAMPLE / "data"

# Kinds and Relations Keeper can load through the built-in import Operations, in load order,
# with the CSV file holding their rows. Contract, PurchaseOrder and RiskFlag are left out: each
# must have a Link (with_supplier, for_part, flags) when it is created, and import_records
# commits Records without Links.
KIND_FILES = {
    "Supplier": "supplier.csv",
    "Part": "part.csv",
    "Plant": "plant.csv",
}
LINK_FILES = {
    "supplied_by": "supplied_by.csv",
    "stocked_at": "stocked_at.csv",
}
DOMAIN = "supply"
WORK = "work"
TRUE_TEXT = {"true": True, "false": False}
DROPPED_OPERATIONS = ("flag_supplier_risk", "approve_contract")
DROPPED_DERIVATION = "single_sourced"


def stripped_schema() -> dict[str, object]:
    """The schema document without the elements that need expressions."""
    document = yaml.safe_load((EXAMPLE / "schema.yaml").read_text())
    for name in DROPPED_OPERATIONS:
        del document["operations"][name]
    del document["derivations"][DROPPED_DERIVATION]
    del document["kinds"]["Part"]["attributes"][DROPPED_DERIVATION]
    return document  # type: ignore[no-any-return]


def stripped_schema_yaml() -> str:
    return yaml.safe_dump(stripped_schema(), sort_keys=False)


def write_stripped_schema(target: Path) -> Path:
    target.write_text(stripped_schema_yaml())
    return target


@dataclass
class Loaded:
    """What loading the example into a Domain produced."""

    base_head: str
    schema_revision: str
    import_revisions: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def attribute_types(schema: dict[str, Any], section: str, name: str) -> dict[str, str]:
    """Attribute api name to type name, of a Kind or Relation of a schema document."""
    for element in schema[section].values():
        if element["api_name"] == name:
            return {a["api_name"]: a["type"]["name"] for a in element["attributes"].values()}
    raise KeyError(name)


def read_rows(file: str, types: dict[str, str], *, links: bool) -> list[dict[str, Any]]:
    """CSV rows as import rows: empty cells left out, booleans as JSON booleans, Link ends
    named `from` and `to`."""
    renamed = {"from_key": "from", "to_key": "to"} if links else {}
    with (DATA / file).open(newline="") as handle:
        return [
            {
                renamed.get(column, column): (
                    TRUE_TEXT[cell] if types.get(column) == "boolean" else cell
                )
                for column, cell in row.items()
                if cell != ""
            }
            for row in csv.DictReader(handle)
        ]
