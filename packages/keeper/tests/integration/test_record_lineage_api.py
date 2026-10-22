"""Record lineage against long histories, past Revisions and branches, in Postgres."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_keeper.reading.record_history import RecordLineageOut
from lorebase_store.materialize.branch import create_branch
from lorebase_store.runtime import StoreRuntime

from .read_fixtures import Seeded, keeper, seeded_database
from .supply import DOMAIN, Clock, commit_changes, load_supply, record_change, rows

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
SIDE = "lineage/side"
RENAMES = 12
"""Changes made to each Supplier after its risk score was set."""


def lineage_url(key: str) -> str:
    return f"{BASE}/kinds/Supplier/records/{key}/lineage"


async def seed(
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema, (_, _, data) = await load_supply(session, runtime, clock)
    supplier = schema.kind_by_api_name("Supplier")
    assert supplier is not None
    originals = {r["supplier_code"]: r for r in rows("supplier.csv")}
    keys = ("S-1001", "S-1002")
    revisions = {"data": data.revision_id.hex()}

    async def change(message: str, **values: str) -> str:
        result = await commit_changes(
            session,
            runtime,
            clock,
            [record_change(supplier, {**originals[key], **values}) for key in keys],
            message=message,
        )
        return result.revision_id.hex()

    revisions["risk"] = await change("Raise the risks", risk_score="90")
    for number in range(RENAMES):
        revisions[f"rename{number}"] = await change(
            f"Rename {number}", risk_score="90", legal_name=f"Name {number}"
        )
    await create_branch(
        session,
        DOMAIN,
        SIDE,
        created_by="usr_editor",
        mode="overlay",
        runtime=runtime,
        clock=clock,
    )
    side = await commit_changes(
        session,
        runtime,
        clock,
        [
            record_change(
                supplier,
                {**originals["S-1001"], "risk_score": "95", "legal_name": f"Name {RENAMES - 1}"},
            )
        ],
        message="Raise the risk on the side branch",
        branch=SIDE,
    )
    revisions["side"] = side.revision_id.hex()
    revisions["final"] = await change("Final name", risk_score="90", legal_name="Final")
    # Rows materialized before the lineage revisions were kept: readers use the history.
    await session.execute(
        text(
            "UPDATE mat_records SET lineage_revisions = NULL "
            "WHERE domain_id = :domain AND key = 'S-1002'"
        ),
        {"domain": DOMAIN},
    )
    await session.commit()
    return Seeded(owner_url=owner_url, app_url=app_url, schema=schema, revisions=revisions)


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


@pytest.fixture
async def http(seeded: Seeded) -> Any:
    async with keeper(seeded.app_url) as client:
        yield client


async def get_lineage(http: httpx.AsyncClient, key: str, **params: str) -> RecordLineageOut:
    response = await http.get(lineage_url(key), params=params)
    assert response.status_code == 200, response.text
    return RecordLineageOut.model_validate(response.json())


def revision_of(lineage: RecordLineageOut, name: str) -> str | None:
    return next(a.revision for a in lineage.attributes if a.name == name)


def value_of(lineage: RecordLineageOut, name: str) -> Any:
    return next(a.value for a in lineage.attributes if a.name == name)


@pytest.mark.parametrize("key", ["S-1001", "S-1002"])
async def test_values_set_before_many_changes_name_their_revision(
    http: httpx.AsyncClient, seeded: Seeded, key: str
) -> None:
    # S-1002 has no lineage revisions stored: the branch history answers the same.
    lineage = await get_lineage(http, key)
    revisions = seeded.revisions
    assert lineage.served_from == "materialized"
    assert lineage.changes == RENAMES + 3
    assert lineage.created is not None
    assert lineage.created.revision == revisions["data"]
    assert lineage.created.change == "created"
    assert "supplier_code" in [a.name for a in lineage.created.attributes]
    assert revision_of(lineage, "supplier_code") == revisions["data"]
    assert revision_of(lineage, "risk_score") == revisions["risk"]
    assert revision_of(lineage, "legal_name") == revisions["final"]
    by_name = {a.name: a for a in lineage.attributes}
    assert by_name["risk_score"].message == "Raise the risks"
    assert by_name["risk_score"].provenance is not None
    assert by_name["risk_score"].provenance.origin_type == "operation"


async def test_lineage_at_a_past_revision_of_the_branch(
    http: httpx.AsyncClient, seeded: Seeded
) -> None:
    revisions = seeded.revisions
    lineage = await get_lineage(http, "S-1001", at=revisions["rename5"])
    assert lineage.served_from == "tree"
    assert lineage.revision == revisions["rename5"]
    assert lineage.changes == 1 + 1 + 6
    assert value_of(lineage, "legal_name") == "Name 5"
    assert revision_of(lineage, "legal_name") == revisions["rename5"]
    assert revision_of(lineage, "risk_score") == revisions["risk"]
    assert revision_of(lineage, "supplier_code") == revisions["data"]
    assert lineage.created is not None
    assert lineage.created.revision == revisions["data"]

    before = await get_lineage(http, "S-1001", at=revisions["data"])
    assert before.changes == 1
    assert revision_of(before, "risk_score") == revisions["data"]
    # the same Revision read from the overlay branch, through its base's history
    side = await get_lineage(http, "S-1001", at=revisions["rename5"], branch=SIDE)
    assert side.attributes == lineage.attributes


async def test_lineage_on_a_branch_and_of_its_revisions(
    http: httpx.AsyncClient, seeded: Seeded
) -> None:
    revisions = seeded.revisions
    head = await get_lineage(http, "S-1001", branch=SIDE)
    assert head.served_from == "materialized"
    assert value_of(head, "risk_score") == "95"
    assert revision_of(head, "risk_score") == revisions["side"]
    assert revision_of(head, "legal_name") == revisions[f"rename{RENAMES - 1}"]
    assert revision_of(head, "supplier_code") == revisions["data"]

    # A Revision of the side branch read on main: its snapshot holds its own first-parent chain.
    walked = await get_lineage(http, "S-1001", at=revisions["side"])
    assert walked.served_from == "tree"
    assert walked.attributes == head.attributes
    assert walked.created is not None
    assert walked.created.revision == revisions["data"]
    assert walked.changes == RENAMES + 3

    main = await get_lineage(http, "S-1001")
    assert value_of(main, "risk_score") == "90"
    assert revision_of(main, "risk_score") == revisions["risk"]
