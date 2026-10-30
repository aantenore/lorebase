"""Source synchronization against Postgres: CSV and Postgres Sources, Proposals, merges."""

from __future__ import annotations

import csv
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from lorebase_core.model.source import MergePolicy as SourcePolicy
from lorebase_scribe.settings import CsvSourceSettings, SourceSettings
from lorebase_scribe.sources import SourceSync
from lorebase_store.approval_rules import ApprovalRuleSettings
from lorebase_store.repositories import DomainRepository, RefRepository, scope_to_domain
from lorebase_store.runtime import StoreRuntime

from .support import (
    AUTO_MERGE,
    DOMAIN,
    EXAMPLE,
    Clock,
    commit_on,
    fetch,
    load_domain,
    record_change,
    rows,
    scalar,
    supply_schema,
)

pytestmark = pytest.mark.integration

SUPPLIERS = "approved.suppliers"
BRANCH = "source/approved.suppliers"


@pytest.fixture
def suppliers_csv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "suppliers.csv"
    shutil.copy(EXAMPLE / "data" / "supplier.csv", path)
    monkeypatch.setenv("SUPPLY_APPROVED_SUPPLIERS_CSV", str(path))
    return path


CONNECTIONS = ("secret://env/SUPPLY_APPROVED_SUPPLIERS_CSV", "secret://env/ERP_DSN")


def settings_for(root: Path) -> SourceSettings:
    return SourceSettings(
        allowed_connections=CONNECTIONS, csv=CsvSourceSettings(allowed_roots=(str(root),))
    )


def rewrite(path: Path, change: Any) -> None:
    """Rewrite the CSV through `change(rows) -> rows`."""
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        data = change(list(reader))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(data)


async def head(sessions: async_sessionmaker[AsyncSession], branch: str) -> bytes | None:
    found = await scalar(
        sessions, "SELECT head FROM refs WHERE domain_id = :d AND name = :b", d=DOMAIN, b=branch
    )
    return None if found is None else bytes(found)


async def count_records(
    sessions: async_sessionmaker[AsyncSession], branch: str, kind: str = "Supplier"
) -> int:
    schema = supply_schema()
    found = schema.kind_by_api_name(kind)
    assert found is not None
    return int(
        await scalar(
            sessions,
            "SELECT count(*) FROM mat_records WHERE domain_id = :d AND branch = :b "
            "AND kind_id = :k",
            d=DOMAIN,
            b=branch,
            k=found.id,
        )
    )


async def test_csv_source_proposal_flow(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    suppliers_csv: Path,
) -> None:
    schema = supply_schema({SUPPLIERS: SourcePolicy.PROPOSAL})
    await load_domain(sessions, runtime, clock, schema)
    sync = SourceSync(
        sessions,
        runtime,
        settings_for(suppliers_csv.parent),
        principal="svc_scribe",
        clock=clock,
        approval_rules=AUTO_MERGE,
    )

    first = await sync.run(DOMAIN, SUPPLIERS)
    assert first.counts["added"] == 30
    assert first.rows_read == 30
    assert first.rows_failed == 0
    assert len(first.revisions) == 1
    assert first.proposal_id is not None
    assert first.proposal_status == "open"
    assert await count_records(sessions, BRANCH) == 30
    assert await count_records(sessions, "main") == 0  # nothing merged under `proposal`
    title = await scalar(sessions, "SELECT title FROM proposals WHERE id = :p", p=first.proposal_id)
    assert title == "Sync approved.suppliers: 30 added, 0 changed, 0 removed"
    # Lineage: each Record names the run and its row.
    origin = await scalar(
        sessions,
        "SELECT p.origin_ref FROM mat_records r, "
        "jsonb_each_text(lb_lineage(r.domain_id, r.lineage)) l "
        "JOIN provenance p ON p.id = l.value "
        "WHERE r.domain_id = :d AND r.branch = :b AND r.key = 'S-1003' LIMIT 1",
        d=DOMAIN,
        b=BRANCH,
    )
    assert origin["type"] == "source_sync"
    assert origin["run_id"] == first.run_id
    assert origin["row_locator"] == "suppliers.csv:4"

    branch_head = await head(sessions, BRANCH)
    attempts = await scalar(sessions, "SELECT count(*) FROM merge_attempts")
    second = await sync.run(DOMAIN, SUPPLIERS)
    assert second.counts["unchanged"] == 30
    assert sum(v for k, v in second.counts.items() if k != "unchanged") == 0
    assert second.revisions == []
    assert second.proposal_id == first.proposal_id
    assert await head(sessions, BRANCH) == branch_head
    assert await scalar(sessions, "SELECT count(*) FROM merge_attempts") == attempts

    def bump(data: list[dict[str, str]]) -> list[dict[str, str]]:
        for row in data:
            if row["supplier_code"] == "S-1003":
                row["risk_score"] = "71"
        return data

    rewrite(suppliers_csv, bump)
    third = await sync.run(DOMAIN, SUPPLIERS)
    assert third.counts["changed"] == 1
    assert third.counts["added"] == 0
    assert third.proposal_id == first.proposal_id
    assert len(third.revisions) == 1
    title = await scalar(sessions, "SELECT title FROM proposals WHERE id = :p", p=first.proposal_id)
    assert title == "Sync approved.suppliers: 0 added, 1 changed, 0 removed"
    assert await scalar(sessions, "SELECT count(*) FROM proposals") == 1

    runs = await fetch(sessions, "SELECT status, proposal_id FROM source_runs ORDER BY started_at")
    assert [(r.status, r.proposal_id) for r in runs] == [("succeeded", first.proposal_id)] * 3


async def test_direct_merge_snapshot_deletes_and_refresh(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    suppliers_csv: Path,
) -> None:
    schema = supply_schema()  # approved.suppliers: auto_when_clean
    await load_domain(sessions, runtime, clock, schema)
    sync = SourceSync(
        sessions,
        runtime,
        settings_for(suppliers_csv.parent),
        principal="svc_scribe",
        clock=clock,
        approval_rules=AUTO_MERGE,
    )

    first = await sync.run(DOMAIN, SUPPLIERS)
    assert first.proposal_status == "merged"
    assert first.merge_status == "merged"
    assert await count_records(sessions, "main") == 30

    again = await sync.run(DOMAIN, SUPPLIERS)
    assert again.revisions == []
    assert again.proposal_id is None

    rewrite(suppliers_csv, lambda data: [r for r in data if r["supplier_code"] != "S-1030"])
    removed = await sync.run(DOMAIN, SUPPLIERS)
    assert removed.counts["removed"] == 1
    assert removed.proposal_status == "merged"
    assert await count_records(sessions, "main") == 29

    # A manual edit on main of an Attribute the Source owns: upstream did not change it, so
    # the next run leaves it (design 9.1 "Corrections that stick").
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    original = next(r for r in rows("supplier.csv") if r["supplier_code"] == "S-1001")
    await commit_on(
        sessions, runtime, clock, [record_change(kind, {**original, "legal_name": "Renamed"})]
    )
    kept = await sync.run(DOMAIN, SUPPLIERS)
    assert kept.counts["changed"] == 0
    assert kept.revisions == []
    legal_name = kind.attribute_by_api_name("legal_name")
    assert legal_name is not None
    value = await scalar(
        sessions,
        "SELECT values ->> :a FROM mat_records WHERE domain_id = :d AND branch = 'main' "
        "AND key = 'S-1001'",
        a=legal_name.id,
        d=DOMAIN,
    )
    assert value == "Renamed"


async def test_dry_run_writes_nothing(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    suppliers_csv: Path,
) -> None:
    await load_domain(sessions, runtime, clock, supply_schema())
    sync = SourceSync(
        sessions,
        runtime,
        settings_for(suppliers_csv.parent),
        principal="svc_scribe",
        clock=clock,
        approval_rules=AUTO_MERGE,
    )
    report = await sync.run(DOMAIN, SUPPLIERS, dry_run=True)
    assert report.counts["added"] == 30
    assert report.revisions == []
    assert report.proposal_id is None
    assert await head(sessions, BRANCH) is None
    stats = await scalar(sessions, "SELECT stats FROM source_runs WHERE id = :r", r=report.run_id)
    assert stats["dry_run"] is True
    assert stats["counts"]["added"] == 30


async def test_bad_rows_are_skipped_and_block_deletions(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    suppliers_csv: Path,
) -> None:
    await load_domain(sessions, runtime, clock, supply_schema())
    sync = SourceSync(
        sessions,
        runtime,
        settings_for(suppliers_csv.parent),
        principal="svc_scribe",
        clock=clock,
        approval_rules=AUTO_MERGE,
    )
    await sync.run(DOMAIN, SUPPLIERS)

    def damage(data: list[dict[str, str]]) -> list[dict[str, str]]:
        data[0]["risk_score"] = "very high"
        return data[:-1]  # S-1030 disappears, but a row failed: no deletion this time

    rewrite(suppliers_csv, damage)
    report = await sync.run(DOMAIN, SUPPLIERS)
    assert report.rows_failed == 1
    assert report.errors[0]["row"] == "suppliers.csv:2"
    assert report.counts["removed"] == 0


async def test_postgres_source(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = supply_schema()  # erp.purchase_orders: incremental, auto_with_authority
    await load_domain(
        sessions,
        runtime,
        clock,
        schema,
        kinds={"Supplier": "supplier.csv", "Part": "part.csv", "Plant": "plant.csv"},
    )
    erp = create_async_engine(database_url, poolclass=NullPool)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        async with erp.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE purchase_orders (po_no text PRIMARY KEY, vendor_id text, "
                    "state text, ordered date, due date, qty integer, price numeric(10,2), "
                    "currency text, part_no text, plant_code text, "
                    "updated_at timestamptz NOT NULL, deleted_at timestamptz)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO purchase_orders VALUES "
                    "('PO-200001','S-1005','ISSUED','2025-11-16','2026-01-10',2500,11.62,'eur',"
                    "'P-10001','PL-10',:t,NULL),"
                    "('PO-200002','S-1001','received','2025-12-01','2026-01-05',5000,24.40,"
                    "'PLN','P-10028','PL-11',:t,NULL),"
                    "('PO-200003','S-1002','draft','2025-12-02',NULL,10,1.00,'EUR',"
                    "'P-10002',NULL,:t,NULL),"
                    "('PO-200004','S-1002','archived','2024-01-02',NULL,10,1.00,'EUR',"
                    "'P-10002',NULL,:t,NULL)"
                ),
                {"t": t0},
            )
        monkeypatch.setenv("ERP_DSN", database_url)
        sync = SourceSync(
            sessions,
            runtime,
            SourceSettings(allowed_connections=CONNECTIONS),
            principal="svc_scribe",
            clock=clock,
            approval_rules=AUTO_MERGE,
        )

        first = await sync.run(DOMAIN, "erp.purchase_orders")
        assert first.rows_read == 3  # the archived order is outside the scope filter
        assert first.counts["added"] == 3
        assert first.counts["links_added"] == 5
        assert first.proposal_status == "merged"
        assert first.watermark == {"type": "timestamp", "value": t0.isoformat()}
        assert await count_records(sessions, "main", "PurchaseOrder") == 3

        async with erp.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE purchase_orders SET state = 'shipped', part_no = 'P-10003', "
                    "updated_at = :t WHERE po_no = 'PO-200001'"
                ),
                {"t": datetime(2026, 2, 1, tzinfo=UTC)},
            )
            await connection.execute(
                text(
                    "UPDATE purchase_orders SET deleted_at = :t, updated_at = :t "
                    "WHERE po_no = 'PO-200003'"
                ),
                {"t": datetime(2026, 2, 2, tzinfo=UTC)},
            )
        second = await sync.run(DOMAIN, "erp.purchase_orders")
        assert second.rows_read == 2  # only rows after the watermark
        assert second.counts["changed"] == 1
        assert second.counts["removed"] == 1
        assert (second.counts["links_added"], second.counts["links_removed"]) == (1, 2)
        assert second.proposal_status == "merged"
        assert await count_records(sessions, "main", "PurchaseOrder") == 2

        third = await sync.run(DOMAIN, "erp.purchase_orders")
        assert third.rows_read == 0
        assert third.revisions == []
    finally:
        await erp.dispose()


async def protect_main(sessions: async_sessionmaker[AsyncSession], **domain_settings: Any) -> None:
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await RefRepository(session, DOMAIN).set_protected("main", True)
        if domain_settings:
            domains = DomainRepository(session)
            domain = await domains.get(DOMAIN)
            assert domain is not None
            await domains.update(DOMAIN, settings={**domain.settings, **domain_settings})


NO_APPROVALS = ApprovalRuleSettings(required_approvals=0)


@pytest.mark.parametrize(
    ("rules", "domain_settings", "held_by"),
    [
        (ApprovalRuleSettings(), {}, ["required_approvals"]),
        (
            ApprovalRuleSettings(required_approvals=0, required_approvals_from_stewards=1),
            {},
            ["required_approvals_from_stewards"],
        ),
        (
            ApprovalRuleSettings(required_approvals=0, required_roles=["data_steward"]),
            {},
            ["required_roles"],
        ),
        (NO_APPROVALS, {"required_approvals": 2}, ["required_approvals"]),
    ],
    ids=["approvals", "stewards", "roles", "domain-override"],
)
async def test_an_auto_merge_waits_for_the_approvals_a_protected_target_requires(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    suppliers_csv: Path,
    rules: ApprovalRuleSettings,
    domain_settings: dict[str, Any],
    held_by: list[str],
) -> None:
    await load_domain(sessions, runtime, clock, supply_schema())  # auto_when_clean
    await protect_main(sessions, **domain_settings)
    sync = SourceSync(
        sessions,
        runtime,
        settings_for(suppliers_csv.parent),
        principal="svc_scribe",
        clock=clock,
        approval_rules=rules,
    )
    report = await sync.run(DOMAIN, SUPPLIERS)
    assert report.proposal_status == "open"
    assert (report.merge_blocked, report.blocking_rules) == ("approvals", held_by)
    assert report.as_dict()["merge_blocked"] == "approvals"
    assert await count_records(sessions, "main") == 0


async def test_an_auto_merge_into_a_protected_target_without_approval_rules_merges(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    suppliers_csv: Path,
) -> None:
    await load_domain(sessions, runtime, clock, supply_schema())
    await protect_main(sessions)
    sync = SourceSync(
        sessions,
        runtime,
        settings_for(suppliers_csv.parent),
        principal="svc_scribe",
        clock=clock,
        approval_rules=NO_APPROVALS,
    )
    report = await sync.run(DOMAIN, SUPPLIERS)
    assert (report.proposal_status, report.merge_blocked) == ("merged", None)
    assert await count_records(sessions, "main") == 30
