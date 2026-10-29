"""Reconciliation detection against Postgres (design 10.6): the Source sync trigger and the
`reconcile_sweep` job, with obsolete candidates and continuations."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import (
    IdPrefix,
    ImportOrigin,
    KindSearch,
    OperationOrigin,
    PeriodMapping,
    ProvenanceEntry,
    Record,
    Schema,
    new_id,
)
from lorebase_core.model.source import MergePolicy as SourcePolicy
from lorebase_core.reconcile import ReconcileOptions
from lorebase_core.revision import Author, Origin
from lorebase_core.types.encoding import format_timestamp
from lorebase_core.validate import RecordDelete, RecordUpsert
from lorebase_inference import ChatResult, InferenceSettings, Usage
from lorebase_inference.fake import embed_text
from lorebase_scribe.embeddings import EmbeddingModels, EmbeddingPass
from lorebase_scribe.extraction.pipeline import ExtractionModels, ExtractionRun
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_scribe.jobs import JobContext, build_handlers
from lorebase_scribe.reconcile.semantic import build_semantic_stage
from lorebase_scribe.reconcile.sweep import ReconcileSweep
from lorebase_scribe.scheduler import Scheduler
from lorebase_scribe.settings import (
    CsvSourceSettings,
    EmbeddingJobSettings,
    SchedulerSettings,
    ScribeSettings,
    SourceSettings,
)
from lorebase_scribe.sources import SourceSync
from lorebase_scribe.worker import PermanentJobError
from lorebase_store.commit import Change, commit
from lorebase_store.jobs import RECONCILE_SWEEP_JOB, JobRow, reconcile_sweep_payload
from lorebase_store.models.enums import ItemDecision
from lorebase_store.runtime import StoreRuntime
from lorebase_store.webhooks import WebhookSettings

from .extraction_support import proposal_items, seed_supply, store_document
from .support import (
    DOMAIN,
    EXAMPLE,
    Clock,
    fetch,
    load_domain,
    record_change,
    rows,
    supply_schema,
)

pytestmark = pytest.mark.integration

SUPPLIERS = "approved.suppliers"
CONNECTIONS = ("secret://env/SUPPLY_APPROVED_SUPPLIERS_CSV",)
Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
def suppliers_csv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "suppliers.csv"
    shutil.copy(EXAMPLE / "data" / "supplier.csv", path)
    monkeypatch.setenv("SUPPLY_APPROVED_SUPPLIERS_CSV", str(path))
    return path


def _supplier(schema: Schema, key: str, **changes: str) -> Record:
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    row = next(r for r in rows("supplier.csv") if r["supplier_code"] == key)
    record = record_change(kind, {**row, **changes}).record
    return record


def _city(schema: Schema) -> str:
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    attribute = kind.attribute_by_api_name("city")
    assert attribute is not None
    return attribute.id


async def _write(
    sessions: Sessions,
    runtime: StoreRuntime,
    clock: Clock,
    change: Any,
    origin: Any,
    actor: str = "usr_editor",
) -> None:
    """Commit `change` attributed to a provenance entry of `origin`."""
    entry = ProvenanceEntry(
        id=new_id(IdPrefix.PROVENANCE), origin=origin, actor=actor, at=format_timestamp(clock())
    )
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            [Change(change, entry.id)],
            author=Author(principal=actor),
            message="Edit",
            origin=Origin(type=origin.type),
            provenance=[entry],
            runtime=runtime,
            clock=clock,
        )


def _operation(principal: str = "usr_editor") -> OperationOrigin:
    return OperationOrigin(invocation_id="inv_1", operation="update_supplier", principal=principal)


async def _candidates(sessions: Sessions) -> list[Any]:
    return await fetch(
        sessions,
        "SELECT type, status, keys, attribute_id, claims, proposal_id, decision, signals "
        "FROM reconciliation_candidates WHERE domain_id = :d ORDER BY created_at, id",
        d=DOMAIN,
    )


async def test_source_sync_raises_contradictions_linked_to_its_proposal(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock, suppliers_csv: Path
) -> None:
    schema = supply_schema({SUPPLIERS: SourcePolicy.PROPOSAL})
    await load_domain(sessions, runtime, clock, schema)
    edited = _supplier(schema, "S-1003", city="Somewhere Else")
    await _write(sessions, runtime, clock, RecordUpsert(record=edited), _operation())

    sync = SourceSync(
        sessions,
        runtime,
        SourceSettings(
            allowed_connections=CONNECTIONS,
            csv=CsvSourceSettings(allowed_roots=(str(suppliers_csv.parent),)),
        ),
        principal="svc_scribe",
        clock=clock,
        reconciliation=ReconcileOptions(),
    )
    report = await sync.run(DOMAIN, SUPPLIERS)
    assert report.counts["reconciliation_raised"] == 1
    (found,) = await _candidates(sessions)
    assert found.type == "contradiction"
    assert found.status == "open"
    assert found.keys == ["S-1003"]
    assert found.attribute_id == _city(schema)
    assert found.proposal_id == report.proposal_id
    source = schema.source_by_api_name(SUPPLIERS)
    assert source is not None
    assert {c["source"] for c in found.claims} == {"principal:usr_editor", f"source:{source.id}"}

    # Running again changes nothing: the candidate is upserted by fingerprint.
    again = await sync.run(DOMAIN, SUPPLIERS)
    assert "reconciliation_raised" not in again.counts
    assert len(await _candidates(sessions)) == 1


async def test_sweep_raises_replacements_resumes_and_retires(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock
) -> None:
    schema = supply_schema()
    await load_domain(sessions, runtime, clock, schema)
    first = _supplier(schema, "S-1003", city="Graz")
    await _write(sessions, runtime, clock, RecordUpsert(record=first), _operation())
    moved = _supplier(schema, "S-1003", city="Vienna")
    await _write(sessions, runtime, clock, RecordUpsert(record=moved), ImportOrigin(batch_id="b1"))
    # The same identity replacing its own value is no disagreement.
    other = _supplier(schema, "S-1004", city="Linz")
    await _write(sessions, runtime, clock, RecordUpsert(record=other), _operation())
    other = _supplier(schema, "S-1004", city="Wels")
    await _write(sessions, runtime, clock, RecordUpsert(record=other), _operation())

    # A tiny budget: the first job stops after the first page and names where to resume.
    tiny = ReconcileOptions(sweep_max_changes_per_job=1)
    sweep = ReconcileSweep(sessions, runtime, tiny, principal="svc_scribe", clock=clock)
    report = await sweep.run(DOMAIN)
    assert report.resume is not None
    assert report.resume[0] == "records"
    jobs = 1
    while report.resume is not None:
        report = await sweep.run(DOMAIN, resume=report.resume)
        jobs += 1
    assert jobs > 2
    (found,) = await _candidates(sessions)
    assert found.type == "contradiction"
    assert found.keys == ["S-1003"]
    assert {c["source"] for c in found.claims} == {"principal:usr_editor", "import:b1"}

    full = ReconcileSweep(
        sessions, runtime, ReconcileOptions(), principal="svc_scribe", clock=clock
    )
    again = await full.run(DOMAIN)
    assert again.complete
    assert again.counts.as_dict().get("raised", 0) == 0
    assert len(await _candidates(sessions)) == 1

    # The Record goes: the next sweep retires the candidate with the reason.
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    await _write(sessions, runtime, clock, RecordDelete(kind=kind.id, key="S-1003"), _operation())
    retired = await full.run(DOMAIN)
    assert retired.counts.as_dict()["obsolete"] == 1
    (found,) = await _candidates(sessions)
    assert found.status == "obsolete"
    assert found.decision["reason"] == "record_deleted"


async def test_sweep_raises_duplicates_and_stale_values(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock
) -> None:
    schema = supply_schema()
    await load_domain(sessions, runtime, clock, schema, kinds={"Supplier": "supplier.csv"})
    original = _supplier(schema, "S-1003")
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    legal = kind.attribute_by_api_name("legal_name")
    vat = kind.attribute_by_api_name("vat_number")
    assert legal is not None
    assert vat is not None
    values = {k: v for k, v in original.values.items() if k != vat.id}
    name = str(original.values[legal.id])
    copy = Record(
        kind=kind.id,
        key="S-1999",
        values={**values, kind.primary_key[0]: "S-1999", legal.id: f"  {name.upper()} "},
    )
    await _write(sessions, runtime, clock, RecordUpsert(record=copy), _operation())

    options = ReconcileOptions(stale={"max_age": "PT0S"})
    sweep = ReconcileSweep(sessions, runtime, options, principal="svc_scribe", clock=clock)
    report = await sweep.run(DOMAIN)
    assert report.complete
    found = await _candidates(sessions)
    duplicates = [c for c in found if c.type == "duplicate"]
    assert any(c.keys == ["S-1003", "S-1999"] for c in duplicates)
    pair = next(c for c in duplicates if c.keys == ["S-1003", "S-1999"])
    assert "normalized" in pair.signals
    assert any(c.type == "stale" for c in found)


class _RiskChat:
    """A tier 2 model stating Kavvik's risk score as the review's on-time rate (81)."""

    model = "risk"

    async def complete_json(self, *_args: Any, **_kwargs: Any) -> ChatResult:
        answer = {
            "records": [
                {
                    "kind": "Supplier",
                    "ref": "S-1025",
                    "evidence": "Kavvik Filtration A.S. | S-1025",
                    "values": {"risk_score": {"value": 81, "evidence": "81 percent"}},
                }
            ],
            "links": [],
        }
        return ChatResult(data=answer, usage=Usage(input_tokens=1, output_tokens=1), model="risk")


async def test_extraction_holds_contested_values_and_raises_candidates(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock
) -> None:
    schema = supply_schema()
    await seed_supply(sessions, runtime, clock, schema)
    run = ExtractionRun(
        sessions,
        runtime,
        ExtractionSettings(tiers=(2,), auto_accept_threshold=0.1, cache=False),
        InferenceSettings(),
        principal="svc_scribe",
        clock=clock,
        models=lambda tiers: ExtractionModels(chat=_RiskChat(), chat_model="test:risk"),
        reconciliation=ReconcileOptions(),
    )
    document = await store_document(sessions, EXAMPLE / "documents" / "supplier-review-2025-q4.md")
    report = await run.run(DOMAIN, [document], requested_by="usr_editor")
    assert report.proposal_id is not None
    assert report.stats["held_by_reconciliation"] == 1
    assert report.stats["reconciliation_raised"] == 1
    items = await proposal_items(sessions, report.proposal_id)
    (item,) = [i for i in items if i.item_type == "value"]
    assert (item.subject["value"], item.subject["current"]) == ("81", "74")
    assert item.decision is ItemDecision.PENDING
    (found,) = await _candidates(sessions)
    assert found.type == "contradiction"
    assert found.proposal_id == report.proposal_id
    assert found.keys == ["S-1025"]
    sources = {c["source"] for c in found.claims}
    assert sources == {"principal:usr_loader", f"document:{document}"}
    proposed = next(c for c in found.claims if "proposal_item" in c)
    assert proposed["evidence"]["quote"] == "81 percent"

    # Without the hold the same item is accepted at once; the candidate is still raised.
    loose = ExtractionRun(
        sessions,
        runtime,
        ExtractionSettings(tiers=(2,), auto_accept_threshold=0.1, cache=False),
        InferenceSettings(),
        principal="svc_scribe",
        clock=clock,
        models=lambda tiers: ExtractionModels(chat=_RiskChat(), chat_model="test:risk"),
        reconciliation=ReconcileOptions(hold_auto_accept=False),
    )
    again = await loose.run(DOMAIN, [document], requested_by="usr_editor")
    assert again.proposal_id is not None
    (accepted,) = [
        i for i in await proposal_items(sessions, again.proposal_id) if i.item_type == "value"
    ]
    assert accepted.decision is ItemDecision.ACCEPTED
    assert "held_by_reconciliation" not in again.stats
    assert again.stats["reconciliation_claim_added"] == 1


async def test_mapped_periods_land_on_the_provenance_of_their_values(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock, suppliers_csv: Path
) -> None:
    schema = supply_schema({SUPPLIERS: SourcePolicy.PROPOSAL})
    city = _city(schema)
    kind = schema.kind_by_api_name("Supplier")
    source = schema.source_by_api_name(SUPPLIERS)
    assert kind is not None
    assert source is not None
    attribute = kind.attributes[city].model_copy(update={"valid_time": True})
    kind = kind.model_copy(update={"attributes": {**kind.attributes, city: attribute}})
    mapping = source.attribute_mapping[city].model_copy(
        update={"valid_from": PeriodMapping(expression='"2025-01-01"')}
    )
    source = source.model_copy(
        update={"attribute_mapping": {**source.attribute_mapping, city: mapping}}
    )
    schema = schema.model_copy(
        update={
            "kinds": {**schema.kinds, kind.id: kind},
            "sources": {**schema.sources, source.id: source},
        }
    )
    await load_domain(sessions, runtime, clock, schema)
    sync = SourceSync(
        sessions,
        runtime,
        SourceSettings(
            allowed_connections=CONNECTIONS,
            csv=CsvSourceSettings(allowed_roots=(str(suppliers_csv.parent),)),
        ),
        principal="svc_scribe",
        clock=clock,
    )
    await sync.run(DOMAIN, SUPPLIERS)
    found = await fetch(
        sessions,
        "SELECT l.key AS attribute, p.valid_from, p.valid_to FROM mat_records r, "
        "jsonb_each_text(lb_lineage(r.domain_id, r.lineage)) l "
        "JOIN provenance p ON p.id = l.value "
        "WHERE r.domain_id = :d AND r.branch = :b AND r.key = 'S-1003'",
        d=DOMAIN,
        b="source/approved.suppliers",
    )
    periods = {row.attribute: (row.valid_from, row.valid_to) for row in found}
    assert periods[city][0] is not None
    assert periods[city][0].isoformat().startswith("2025-01-01")
    assert periods[city][1] is None
    assert all(v == (None, None) for a, v in periods.items() if a != city)


async def test_sweep_job_continues_and_is_scheduled(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime, clock: Clock
) -> None:
    await load_domain(sessions, runtime, clock, supply_schema(), kinds={"Supplier": "supplier.csv"})
    async with httpx.AsyncClient() as http:
        context = JobContext(
            engine=engine,
            sessions=sessions,
            runtime=runtime,
            settings=ScribeSettings.model_validate(
                {"reconciliation": {"sweep_max_changes_per_job": 1}}
            ),
            webhooks=WebhookSettings(),
            http=http,
            worker_id="test",
            clock=clock,
        )
        handler = build_handlers(context, [RECONCILE_SWEEP_JOB])[RECONCILE_SWEEP_JOB]
        now = datetime.now(UTC)
        job = JobRow(1, RECONCILE_SWEEP_JOB, DOMAIN, reconcile_sweep_payload(), 0, 1, now, "t", now)
        result = await handler(job)
        assert result["resume"] is not None
        (payload,) = await fetch(
            sessions, "SELECT payload FROM jobs WHERE id = :id", id=result["continuation"]
        )
        assert payload[0] == {"resume": result["resume"]}
        bad = JobRow(2, RECONCILE_SWEEP_JOB, DOMAIN, {"resume": [1]}, 0, 1, now, "t", now)
        with pytest.raises(PermanentJobError):
            await handler(bad)

    scheduler = Scheduler(
        engine,
        sessions,
        settings=SchedulerSettings(
            webhook_interval_s=0,
            gc_schedule=None,
            embeddings_interval_s=0,
            audit_verify_interval_s=0,
            source_schedules=False,
        ),
        jobs=runtime.settings.jobs,
        writes=runtime.settings.writes,
        reconcile_interval_s=60,
    )
    try:
        first = await scheduler.tick()
        assert len(first) == 1
        assert await scheduler.tick() == []  # within the interval
    finally:
        await scheduler.release()
    queued = await fetch(
        sessions, "SELECT type, domain_id, payload FROM jobs WHERE id = :id", id=first[0]
    )
    assert [tuple(r) for r in queued] == [(RECONCILE_SWEEP_JOB, DOMAIN, {})]


class _FoldedModel:
    """The fake embedding model over case- and space-folded texts."""

    name = "fake:folded"

    def __init__(self, dims: int) -> None:
        self.dims = dims

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [embed_text(" ".join(t.split()).casefold(), self.dims) for t in texts]


async def test_sweep_raises_duplicates_from_stored_vectors(
    engine: AsyncEngine, sessions: Sessions, runtime: StoreRuntime, clock: Clock
) -> None:
    schema = supply_schema()
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    kind = kind.model_copy(update={"search": KindSearch(embedding_template="{legal_name}")})
    schema = schema.model_copy(update={"kinds": {**schema.kinds, kind.id: kind}})
    await load_domain(sessions, runtime, clock, schema, kinds={"Supplier": "supplier.csv"})
    original = _supplier(schema, "S-1003")
    legal = kind.attribute_by_api_name("legal_name")
    vat = kind.attribute_by_api_name("vat_number")
    assert legal is not None
    assert vat is not None
    values = {k: v for k, v in original.values.items() if k != vat.id}
    name = str(original.values[legal.id])
    copy = Record(
        kind=kind.id,
        key="S-1999",
        values={**values, kind.primary_key[0]: "S-1999", legal.id: f"  {name.upper()} "},
    )
    await _write(sessions, runtime, clock, RecordUpsert(record=copy), _operation())
    dims = runtime.settings.embeddings.dims
    model = _FoldedModel(dims)
    models = EmbeddingModels(InferenceSettings(), dims=dims, factory=lambda: model)
    embedded = await EmbeddingPass(
        engine, sessions, models, EmbeddingJobSettings(), dims=dims, clock=clock
    ).run(DOMAIN)
    assert embedded.complete

    # Identity left out: only the stored vectors can pair the two Records.
    options = ReconcileOptions(
        stages=["values", "normalize", "semantic"], semantic={"enabled": True}
    )
    stage = build_semantic_stage(options, models)
    sweep = ReconcileSweep(
        sessions, runtime, options, principal="svc_scribe", clock=clock, semantic=stage
    )
    report = await sweep.run(DOMAIN)
    assert report.complete
    duplicates = [c for c in await _candidates(sessions) if c.type == "duplicate"]
    assert [c.keys for c in duplicates] == [["S-1003", "S-1999"]]
    assert duplicates[0].signals["semantic"]["similarity"] == pytest.approx(1.0)
