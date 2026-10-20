"""A fixed context pack draft over the `supply` schema, shared by the budget and markdown tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from lorebase_core.model import Schema
from lorebase_core.model.yaml_schema import load_schema
from lorebase_keeper.agents.budget import (
    ChangeItem,
    Draft,
    LineageItem,
    LinkItem,
    NeighborItem,
    SeedItem,
)

EXAMPLE = Path(__file__).resolve().parents[4] / "examples" / "supply"
REV_A = "a" * 64
REV_B = "b" * 64


@pytest.fixture(scope="session")
def schema() -> Schema:
    return load_schema((EXAMPLE / "schema.yaml").read_text())


@pytest.fixture
def draft() -> Draft:
    """Supplier/S-1001 with near and far neighbors, Links, changes and lineage."""
    return Draft(
        seeds=(
            SeedItem(
                ref="Supplier/S-1001",
                kind="Supplier",
                title="Kavsen Alloys Sp. z o.o.",
                values={
                    "supplier_code": "S-1001",
                    "legal_name": "Kavsen Alloys Sp. z o.o.",
                    "country": "PL",
                    "city": "Poznan",
                    "tier": "1",
                    "risk_score": "90",
                    "status": "active",
                },
                updated_revision=REV_B,
            ),
        ),
        neighbors=(
            NeighborItem(
                ref="Contract/C-3001",
                kind="Contract",
                title="Framework supply agreement 2023 - Kavsen Alloys Sp. z o.o.",
                depth=1,
                values={"contract_number": "C-3001", "status": "active", "currency": "PLN"},
            ),
            NeighborItem(
                ref="Part/P-10028",
                kind="Part",
                title="Radial shaft seal 25x42x7",
                depth=1,
                values={"part_number": "P-10028", "category": "seal", "unit_cost": "24.01"},
            ),
            NeighborItem(
                ref="Plant/PL-03",
                kind="Plant",
                title="Wroclaw assembly",
                depth=2,
                values={"plant_code": "PL-03", "plant_type": "assembly", "country": "PL"},
            ),
            NeighborItem(
                ref="Plant/PL-05",
                kind="Plant",
                title="Lyon hub",
                depth=2,
                values={"plant_code": "PL-05", "plant_type": "distribution_hub"},
            ),
        ),
        links=(
            LinkItem("stocked_at", "Part/P-10028", "Plant/PL-03", {"quantity": "120"}),
            LinkItem("stocked_at", "Part/P-10028", "Plant/PL-05", {"quantity": "40"}),
            LinkItem(
                "supplied_by",
                "Part/P-10028",
                "Supplier/S-1001",
                {"is_primary": True, "lead_time_days": "21"},
            ),
            LinkItem("with_supplier", "Contract/C-3001", "Supplier/S-1001"),
        ),
        schema=True,
        changes=(
            ChangeItem(
                REV_A,
                "2026-10-01T08:00:03Z",
                "Supplier/S-1001",
                "added",
                ("supplier_code", "legal_name", "country", "city", "tier", "risk_score"),
                "usr_loader",
                "import",
                "Import the supply example data",
            ),
            ChangeItem(
                REV_B,
                "2026-10-01T08:00:04Z",
                "Supplier/S-1001",
                "modified",
                ("risk_score",),
                "usr_editor",
                "operation",
                "Raise the risk of S-1001",
            ),
        ),
        lineage=(
            LineageItem(
                "Supplier/S-1001",
                "import",
                "usr_loader",
                "2026-10-01T08:00:03Z",
                ("supplier_code", "legal_name", "country", "city", "tier", "status"),
            ),
            LineageItem(
                "Supplier/S-1001",
                "operation",
                "usr_editor",
                "2026-10-01T08:00:04Z",
                ("risk_score",),
            ),
        ),
    )
