"""Record lineage against long histories, past Revisions and branches, in Postgres."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import new_ulid
from lorebase_keeper.reading.record_history import RecordLineageOut
from lorebase_store.materialize.branch import create_branch
from lorebase_store.merging import MergeRequest, MergeService
from lorebase_store.models.enums import ProposalOrigin, ProposalStatus
from lorebase_store.repositories import ProposalRepository, ProposalRow, RefRepository
from lorebase_store.runtime import StoreRuntime

from .read_fixtures import Seeded, keeper, seeded_database
from .supply import DOMAIN, Clock, commit_changes, load_supply, record_change, rows

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
SIDE = "lineage/side"
MERGED = "lineage/merged"
FORWARD = "lineage/forward"
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

    async def change_one(key: str, message: str, **values: str) -> str:
        result = await commit_changes(
            session,
            runtime,
            clock,
            [record_change(supplier, {**originals[key], **values})],
            message=message,
        )
        return result.revision_id.hex()

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

    async def merge(source: str, title: str) -> tuple[str, str]:
        now = clock()
        proposal = ProposalRow(
            id=f"prp_{new_ulid()}",
            title=title,
            description=None,
            source_ref=source,
            target_ref="main",
            origin=ProposalOrigin.MANUAL,
            status=ProposalStatus.OPEN,
            author="usr_editor",
            created_at=now,
            checks={},
            policies={},
            updated_at=now,
        )
        await ProposalRepository(session, DOMAIN).insert(proposal)
        result = await MergeService(session, DOMAIN, runtime=runtime, clock=clock).merge(
            MergeRequest(proposal=proposal, actor="usr_maintainer")
        )
        assert result.merged
        main = await RefRepository(session, DOMAIN).get("main")
        assert main is not None
        await session.commit()
        return proposal.id, main.head.hex()

    def supplier_row(key: str, **values: str) -> Any:
        return record_change(supplier, {**originals[key], **values})

    # A value changed on a side branch and merged into main (a merge Revision), and one
    # fast-forwarded into main.
    await create_branch(
        session, DOMAIN, MERGED, created_by="usr_editor", runtime=runtime, clock=clock
    )
    merged_side = await commit_changes(
        session,
        runtime,
        clock,
        [supplier_row("S-1005", risk_score="77")],
        message="Raise S-1005 on a side branch",
        branch=MERGED,
    )
    revisions["merged_side"] = merged_side.revision_id.hex()
    await change_one("S-1006", "Lower S-1006 on main", risk_score="11")
    revisions["merge_proposal"], revisions["merge"] = await merge(MERGED, "Merge the side")
    await create_branch(
        session, DOMAIN, FORWARD, created_by="usr_editor", runtime=runtime, clock=clock
    )
    forward = await commit_changes(
        session,
        runtime,
        clock,
        [supplier_row("S-1007", risk_score="66")],
        message="Raise S-1007 on a side branch",
        branch=FORWARD,
    )
    revisions["forward"] = forward.revision_id.hex()
    revisions["forward_proposal"], head = await merge(FORWARD, "Forward the side")
    assert head == revisions["forward"]

    # The change that created S-1003 names a Revision without a history row; S-1004 holds
    # JSON that is not an object where maps are expected.
    await session.execute(
        text(
            "UPDATE mat_records SET lineage_revisions = "
            "jsonb_set(lineage_revisions, '{$created}', '\"00\"') "
            "WHERE domain_id = :domain AND branch = 'main' AND key = 'S-1003'"
        ),
        {"domain": DOMAIN},
    )
    await session.execute(
        text(
            "UPDATE mat_records SET lineage = 'null'::jsonb, lineage_revisions = '[]'::jsonb "
            "WHERE domain_id = :domain AND branch = 'main' AND key = 'S-1004'"
        ),
        {"domain": DOMAIN},
    )
    await session.execute(
        text(
            "UPDATE mat_history SET provenance = '\"text\"'::jsonb "
            "WHERE domain_id = :domain AND branch = 'main' AND element_key = 'S-1004' "
            "AND old_hash IS NULL"
        ),
        {"domain": DOMAIN},
    )
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


def by_name(lineage: RecordLineageOut) -> dict[str, Any]:
    return {a.name: a for a in lineage.attributes}


async def test_a_value_merged_into_main_names_the_revision_that_brought_it(
    http: httpx.AsyncClient, seeded: Seeded
) -> None:
    revisions = seeded.revisions
    side = by_name(await get_lineage(http, "S-1005", branch=MERGED))
    assert side["risk_score"].revision == revisions["merged_side"]

    # A merge Revision: the value keeps the provenance of the side change and names the merge
    # Revision on main's first-parent chain, and the Proposal merged in it.
    merged = by_name(await get_lineage(http, "S-1005"))
    risk = merged["risk_score"]
    assert risk.value == "77"
    assert risk.revision == revisions["merge"]
    assert risk.provenance is not None
    assert risk.provenance.id == side["risk_score"].provenance.id
    assert risk.provenance.origin_type == "operation"
    assert risk.proposal is not None
    assert risk.proposal.id == revisions["merge_proposal"]
    assert risk.proposal.merged_revision == revisions["merge"]
    # Values the merge left as they were keep the Revision that set them.
    assert merged["supplier_code"].revision == revisions["data"]
    assert merged["legal_name"].revision == revisions["data"]
    assert merged["supplier_code"].proposal is None

    # A fast-forward: main's chain is the side branch's, so the side Revision is named.
    forward = by_name(await get_lineage(http, "S-1007"))
    assert forward["risk_score"].value == "66"
    assert forward["risk_score"].revision == revisions["forward"]
    assert forward["risk_score"].proposal is not None
    assert forward["risk_score"].proposal.id == revisions["forward_proposal"]
    assert forward["supplier_code"].revision == revisions["data"]


async def test_the_creating_change_falls_back_to_the_history(
    http: httpx.AsyncClient, seeded: Seeded
) -> None:
    # `$created` names a Revision with no history row for the Record.
    lineage = await get_lineage(http, "S-1003")
    assert lineage.created is not None
    assert lineage.created.revision == seeded.revisions["data"]
    assert revision_of(lineage, "supplier_code") == seeded.revisions["data"]


async def test_maps_that_are_not_objects_read_as_empty(
    http: httpx.AsyncClient, seeded: Seeded
) -> None:
    lineage = await get_lineage(http, "S-1004")
    assert value_of(lineage, "supplier_code") == "S-1004"
    assert all(a.provenance is None and a.revision is None for a in lineage.attributes)
    assert lineage.created is not None
    assert lineage.created.revision == seeded.revisions["data"]
    assert lineage.created.attributes
    assert all(a.provenance is None for a in lineage.created.attributes)


async def test_history_scans_and_counts_are_bounded(seeded: Seeded) -> None:
    revisions = seeded.revisions
    async with keeper(seeded.app_url) as http:
        unbounded = await get_lineage(http, "S-1001")
    assert (unbounded.changes, unbounded.changes_truncated) == (RENAMES + 3, False)

    bounds = {"history": {"lineage_scan_changes": 3, "lineage_count_max": 5}}
    async with keeper(seeded.app_url, read=bounds) as http:
        # Stored lineage revisions need no scan.
        stored = await get_lineage(http, "S-1001")
        assert (stored.changes, stored.changes_truncated) == (5, True)
        assert revision_of(stored, "risk_score") == revisions["risk"]
        assert stored.created is not None
        assert stored.created.revision == revisions["data"]
        # Without them, only the newest three changes are searched.
        fallback = await get_lineage(http, "S-1002")
        assert (fallback.changes, fallback.changes_truncated) == (5, True)
        assert revision_of(fallback, "legal_name") == revisions["final"]
        assert revision_of(fallback, "risk_score") is None
        assert by_name(fallback)["risk_score"].provenance is not None
        assert fallback.created is None
