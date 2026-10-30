"""Incremental Sources and upstream deletions (design 9.1 "Incremental Sources"): each run
records the Records it read (`source_written.last_seen_run`), and the reconciliation sweep
raises a `stale` candidate (`source_unseen`) for a Record no run has seen for longer than the
Source's `stale_after`, never deleting it; the candidate goes obsolete once a run sees the
Record again."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from lorebase_core.model import Schema
from lorebase_core.reconcile import ReconcileOptions
from lorebase_scribe.reconcile.sweep import ReconcileSweep
from lorebase_scribe.settings import SourceSettings
from lorebase_scribe.sources import SourceSync
from lorebase_store.runtime import StoreRuntime

from .support import AUTO_MERGE, DOMAIN, Clock, fetch, load_domain, supply_schema

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
ORDERS = "erp.purchase_orders"
CONNECTIONS = ("secret://env/SUPPLY_APPROVED_SUPPLIERS_CSV", "secret://env/ERP_DSN")
ROW = "'S-1005','ISSUED','2025-11-16','2026-01-10',10,1.00,'EUR','P-10001','PL-10'"


class Shifted:
    """A strictly increasing clock that can jump forward."""

    def __init__(self) -> None:
        self._clock = Clock()
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return self._clock() + self.offset


def with_stale_after(schema: Schema, value: str) -> Schema:
    source = schema.source_by_api_name(ORDERS)
    assert source is not None
    updated = source.model_copy(update={"stale_after": value})
    return schema.model_copy(update={"sources": {**schema.sources, source.id: updated}})


async def _seen(sessions: Sessions) -> dict[str, str | None]:
    found = await fetch(
        sessions,
        "SELECT key, last_seen_run FROM source_written WHERE domain_id = :d AND element = :k",
        d=DOMAIN,
        k=_kind_id(),
    )
    return {row.key: row.last_seen_run for row in found}


def _kind_id() -> str:
    kind = supply_schema().kind_by_api_name("PurchaseOrder")
    assert kind is not None
    return kind.id


async def _candidates(sessions: Sessions) -> list[Any]:
    return await fetch(
        sessions,
        "SELECT type, status, keys, attribute_id, claims, decision, signals "
        "FROM reconciliation_candidates WHERE domain_id = :d ORDER BY created_at, id",
        d=DOMAIN,
    )


def _sweep(
    sessions: Sessions, runtime: StoreRuntime, clock: Callable[[], datetime], **stale: Any
) -> ReconcileSweep:
    options = ReconcileOptions(stale=stale) if stale else ReconcileOptions()
    return ReconcileSweep(sessions, runtime, options, principal="svc_scribe", clock=clock)


async def test_records_unseen_by_an_incremental_source_become_stale_candidates(
    sessions: Sessions,
    runtime: StoreRuntime,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Shifted()
    schema = with_stale_after(supply_schema(), "P1D")
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
                    f"INSERT INTO purchase_orders VALUES ('PO-300001',{ROW},:t,NULL),"
                    f"('PO-300002',{ROW},:t,NULL)"
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

        first = await sync.run(DOMAIN, ORDERS)
        assert first.proposal_status == "merged"
        assert await _seen(sessions) == {"PO-300001": first.run_id, "PO-300002": first.run_id}

        # Two days later only PO-300001 changed upstream: the run reads it alone.
        clock.offset = timedelta(days=2)
        async with erp.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE purchase_orders SET qty = 11, updated_at = :t WHERE po_no = 'PO-300001'"
                ),
                {"t": t0 + timedelta(days=2)},
            )
        second = await sync.run(DOMAIN, ORDERS)
        assert second.rows_read == 1
        assert await _seen(sessions) == {"PO-300001": second.run_id, "PO-300002": first.run_id}

        # The check is off for the operator: nothing is raised.
        off = await _sweep(sessions, runtime, clock, source_unseen=False).run(DOMAIN)
        assert off.complete
        assert not [c for c in await _candidates(sessions) if "source_unseen" in c.signals]

        # PO-300002 has not been seen for two days, longer than stale_after (P1D).
        report = await _sweep(sessions, runtime, clock).run(DOMAIN)
        assert report.complete
        unseen = [c for c in await _candidates(sessions) if "source_unseen" in c.signals]
        assert len(unseen) == 1
        (found,) = unseen
        assert found.type == "stale"
        assert found.status == "open"
        assert found.keys == ["PO-300002"]
        assert found.attribute_id is None
        assert found.claims == []
        assert found.signals["stale"] == ["source_unseen"]
        signal = found.signals["source_unseen"]
        assert signal["last_seen_run"] == first.run_id
        assert signal["stale_after"] == "P1D"

        # Nothing was deleted, and a second sweep folds into the same candidate.
        again = await _sweep(sessions, runtime, clock).run(DOMAIN)
        assert again.counts.as_dict().get("raised", 0) == 0
        assert len([c for c in await _candidates(sessions) if "source_unseen" in c.signals]) == 1
        assert set(await _seen(sessions)) == {"PO-300001", "PO-300002"}

        # Upstream touches PO-300002: the next run sees it and the sweep retires the candidate.
        clock.offset = timedelta(days=3)
        async with erp.begin() as connection:
            await connection.execute(
                text("UPDATE purchase_orders SET updated_at = :t WHERE po_no = 'PO-300002'"),
                {"t": t0 + timedelta(days=3)},
            )
        third = await sync.run(DOMAIN, ORDERS)
        assert (await _seen(sessions))["PO-300002"] == third.run_id
        retired = await _sweep(sessions, runtime, clock).run(DOMAIN)
        assert retired.counts.as_dict()["obsolete"] == 1
        by_key = {c.keys[0]: c for c in await _candidates(sessions) if "source_unseen" in c.signals}
        assert by_key["PO-300002"].status == "obsolete"
        assert by_key["PO-300002"].decision["reason"] == "source_seen"
        # A day after its last sighting, PO-300001 is the one unseen now.
        assert by_key["PO-300001"].status == "open"
        assert by_key["PO-300001"].signals["source_unseen"]["last_seen_run"] == second.run_id
    finally:
        async with erp.begin() as connection:
            await connection.execute(text("DROP TABLE IF EXISTS purchase_orders"))
        await erp.dispose()
