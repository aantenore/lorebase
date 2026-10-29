"""The evidence check of document versions and retirements against Postgres (design 9.2,
job `document_version_check`): `stale` candidates when a quote is gone, none when it is still
there (white space aside) or the new version proposes the value again, retirement, masking of
sensitive quotes, continuations and the block on extracting a retired document."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import (
    ExtractionOrigin,
    IdPrefix,
    ProvenanceEntry,
    Schema,
    new_id,
    new_ulid,
)
from lorebase_core.reconcile import ReconcileOptions, TriggerOptions
from lorebase_core.revision import Author, Origin
from lorebase_core.tree import kind_partition
from lorebase_core.types.encoding import format_timestamp
from lorebase_core.validate import RecordUpsert
from lorebase_inference import InferenceSettings
from lorebase_scribe.extraction.parsing import MARKDOWN
from lorebase_scribe.extraction.pipeline import DocumentRetiredError, ExtractionRun
from lorebase_scribe.extraction.settings import ExtractionSettings, VersionCheckSettings
from lorebase_scribe.reconcile.versions import DocumentVersionCheck
from lorebase_store.blobs import TEXT_REF_KEY, PostgresBlobStore
from lorebase_store.commit import Change, commit
from lorebase_store.models.enums import ItemDecision, ProposalOrigin, ProposalStatus, RunStatus
from lorebase_store.repositories import (
    DocumentRepository,
    DocumentRow,
    ExtractionJobRepository,
    ExtractionJobRow,
    ItemRow,
    ProposalItemRepository,
    ProposalRepository,
    ProposalRow,
    scope_to_domain,
)
from lorebase_store.repositories.proposal_items import VALUE_ITEM
from lorebase_store.runtime import StoreRuntime

from .support import DOMAIN, Clock, fetch, load_domain, record_change, rows, supply_schema

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
KEY = "reviews/suppliers.md"


async def store_version(
    sessions: Sessions, text: str, *, key: str | None = KEY, parsed: bool = True
) -> DocumentRow:
    """Store `text` as a document (a version of `key`, or keyless) with its parsed text."""
    data = text.encode()
    digest = hashlib.sha256(data).digest()
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        ref = await PostgresBlobStore().put(session, DOMAIN, data)
        repository = DocumentRepository(session, DOMAIN)
        row = DocumentRow(
            id=f"doc_{new_ulid()}",
            filename="suppliers.md",
            media_type=MARKDOWN,
            sha256=digest,
            size=len(data),
            blob_ref=ref,
            created_by="usr_loader",
            source_key=key,
        )
        stored = (
            (await repository.add(row))[0]
            if key is None
            else (await repository.add_version(row)).row
        )
        if parsed:
            await repository.set_text(stored.id, text_hash=digest, metadata={TEXT_REF_KEY: ref})
        found = await repository.get(stored.id)
    assert found is not None
    return found


def attribute_id(schema: Schema, name: str) -> str:
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    found = kind.attribute_by_api_name(name)
    assert found is not None
    return found.id


async def extracted(
    sessions: Sessions,
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
    document: DocumentRow,
    values: list[tuple[str, str, Any, str]],
    *,
    commit_values: bool = True,
    decision: ItemDecision = ItemDecision.ACCEPTED,
) -> None:
    """What an extraction of `document` merged into `main`: a job, its merged Proposal, one
    accepted value item per `(key, attribute name, value, quote)` and, with `commit_values`,
    the values committed on `main` under `extraction` provenance."""
    kind = schema.kind_by_api_name("Supplier")
    assert kind is not None
    job_id, proposal_id = f"exj_{new_ulid()}", f"prp_{new_ulid()}"
    now = clock()
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await ProposalRepository(session, DOMAIN).insert(
            ProposalRow(
                id=proposal_id,
                title="Extraction",
                description=None,
                source_ref=f"extract/{job_id}",
                target_ref="main",
                origin=ProposalOrigin.EXTRACTION,
                status=ProposalStatus.MERGED,
                author="svc_scribe",
                created_at=now,
            )
        )
        await ExtractionJobRepository(session, DOMAIN).insert(
            ExtractionJobRow(
                id=job_id,
                documents=[document.id],
                schema_hash=b"\x00" * 32,
                model="rules",
                prompt_version="extract-v1",
                status=RunStatus.SUCCEEDED,
                proposal_id=proposal_id,
            )
        )
        await ProposalItemRepository(session, DOMAIN).insert(
            proposal_id,
            [
                ItemRow(
                    item_id=f"pit_{seq}",
                    item_type=VALUE_ITEM,
                    seq=seq,
                    subject={
                        "kind": kind.id,
                        "key": key,
                        "attribute": attribute_id(schema, name),
                        "value": value,
                    },
                    partition=kind_partition(kind.id),
                    element_key=key,
                    attribute_id=attribute_id(schema, name),
                    evidence={
                        "document_id": document.id,
                        "chunk": 0,
                        "start": 0,
                        "end": 1,
                        "quote": quote,
                    },
                    decision=decision,
                    job_id=job_id,
                )
                for seq, (key, name, value, quote) in enumerate(values)
            ],
        )
    if not commit_values:
        return
    changes: list[Change] = []
    entries: list[ProvenanceEntry] = []
    for key, name, value, _quote in values:
        row = next(r for r in rows("supplier.csv") if r["supplier_code"] == key)
        record = record_change(kind, row).record
        record = record.model_copy(
            update={"values": {**record.values, attribute_id(schema, name): value}}
        )
        entry = ProvenanceEntry(
            id=new_id(IdPrefix.PROVENANCE),
            origin=ExtractionOrigin(
                job_id=job_id,
                document_id=document.id,
                span_start=0,
                span_end=1,
                model_id="rules",
                prompt_version="extract-v1",
            ),
            actor="svc_scribe",
            at=format_timestamp(clock()),
        )
        entries.append(entry)
        changes.append(Change(RecordUpsert(record=record), entry.id))
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            changes,
            author=Author(principal="svc_scribe"),
            message="Merge extraction",
            origin=Origin(type="extraction"),
            provenance=entries,
            runtime=runtime,
            clock=clock,
        )


def checker(
    sessions: Sessions,
    runtime: StoreRuntime,
    clock: Clock,
    *,
    max_values: int = 5000,
    options: ReconcileOptions | None = None,
) -> DocumentVersionCheck:
    return DocumentVersionCheck(
        sessions,
        runtime,
        ExtractionSettings(versions=VersionCheckSettings(max_values_per_job=max_values)),
        options or ReconcileOptions(),
        principal="svc_scribe",
        clock=clock,
    )


async def candidates(sessions: Sessions) -> list[Any]:
    return await fetch(
        sessions,
        "SELECT type, status, keys, attribute_id, claims, signals FROM reconciliation_candidates "
        "WHERE domain_id = :d ORDER BY created_at, id",
        d=DOMAIN,
    )


@pytest.fixture
async def schema(sessions: Sessions, runtime: StoreRuntime, clock: Clock) -> Schema:
    found = supply_schema()
    await load_domain(sessions, runtime, clock, found, kinds={"Supplier": "supplier.csv"})
    return found


async def test_a_new_version_raises_stale_only_where_the_quote_is_gone(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock, schema: Schema
) -> None:
    first = await store_version(
        sessions, "Kavsen Alloys is based in Gdansk.\nSundello Seals is   located\nin Lodz."
    )
    await extracted(
        sessions,
        runtime,
        clock,
        schema,
        first,
        [
            ("S-1001", "city", "Gdansk", "Kavsen Alloys is based in Gdansk."),
            ("S-1002", "city", "Lodz", "Sundello Seals is located in Lodz."),
            ("S-1003", "city", "Torun", "Third supplier works from Torun."),
        ],
    )
    second = await store_version(
        sessions, "Sundello Seals is located in\n Lodz.\nThe third one now says Torun."
    )
    assert (second.version, second.replaces) == (2, first.id)
    # The new version's own extraction proposes S-1003's value again: it is supported.
    await extracted(
        sessions,
        runtime,
        clock,
        schema,
        second,
        [("S-1003", "city", "Torun", "The third one now says Torun.")],
        commit_values=False,
        decision=ItemDecision.PENDING,
    )
    report = await checker(sessions, runtime, clock).run(DOMAIN, second.id, mode="version")
    counts = report.counts.as_dict()
    assert (counts["raised"], counts["present"], counts["supported"]) == (1, 1, 1)
    found = await candidates(sessions)
    assert len(found) == 1
    candidate = found[0]
    assert (candidate.type, candidate.status, candidate.keys) == ("stale", "open", ["S-1001"])
    assert candidate.signals["stale"] == ["evidence_gone"]
    assert candidate.signals["evidence_gone"] == {
        "document": first.id,
        "from_version": 1,
        "to_version": 2,
        "quote": "Kavsen Alloys is based in Gdansk.",
    }
    assert candidate.claims[0]["evidence"]["quote"] == "Kavsen Alloys is based in Gdansk."
    assert candidate.claims[0]["source"] == f"document:{first.id}"
    assert candidate.claims[0]["value"] == "Gdansk"  # the current value, untouched
    again = await checker(sessions, runtime, clock).run(DOMAIN, second.id, mode="version")
    assert "raised" not in again.counts.as_dict()
    assert len(await candidates(sessions)) == 1


async def test_retirement_raises_for_every_value_and_blocks_extraction(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock, schema: Schema
) -> None:
    document = await store_version(sessions, "Kavsen Alloys is based in Gdansk.", key=None)
    await extracted(
        sessions,
        runtime,
        clock,
        schema,
        document,
        [
            ("S-1001", "city", "Gdansk", "Kavsen Alloys is based in Gdansk."),
            ("S-1002", "legal_name", "Sundello", "Kavsen Alloys is based in Gdansk."),
        ],
    )
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        retired = await DocumentRepository(session, DOMAIN).retire(
            document.id, by="usr_admin", at=clock()
        )
    assert retired is not None
    assert retired.status == "retired"
    report = await checker(sessions, runtime, clock).run(DOMAIN, document.id, mode="retire")
    assert report.counts.as_dict()["raised"] == 2
    found = await candidates(sessions)
    assert sorted(c.keys[0] for c in found) == ["S-1001", "S-1002"]
    assert all(c.signals["evidence_gone"]["to_version"] is None for c in found)
    run = ExtractionRun(
        sessions,
        runtime,
        ExtractionSettings(tiers=(0,)),
        InferenceSettings(),
        principal="svc_scribe",
        clock=clock,
    )
    with pytest.raises(DocumentRetiredError):
        await run.run(DOMAIN, [document.id])


async def test_quotes_of_sensitive_attributes_are_wrapped(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock, schema: Schema
) -> None:
    first = await store_version(sessions, "Kavsen Alloys has VAT PL999.")
    await extracted(
        sessions, runtime, clock, schema, first, [("S-1001", "vat_number", "PL999", "VAT PL999")]
    )
    second = await store_version(sessions, "Nothing about VAT any more.")
    await checker(sessions, runtime, clock).run(DOMAIN, second.id, mode="version")
    (candidate,) = await candidates(sessions)
    wrapped = candidate.claims[0]["evidence"]["quote"]
    assert wrapped == {"$sensitive": {"tags": ["confidential"], "value": "VAT PL999"}}
    assert candidate.signals["evidence_gone"]["quote"] == wrapped
    assert candidate.claims[0]["evidence"]["document_id"] == first.id


async def test_checks_continue_past_the_limit_and_follow_the_domain_options(
    sessions: Sessions, runtime: StoreRuntime, clock: Clock, schema: Schema
) -> None:
    first = await store_version(sessions, "a b c")
    await extracted(
        sessions,
        runtime,
        clock,
        schema,
        first,
        [("S-1001", "city", "Gdansk", "q1"), ("S-1002", "city", "Lodz", "q2")],
    )
    second = await store_version(sessions, "none of it")
    off = ReconcileOptions(triggers=TriggerOptions(document_versions=False))
    skipped = await checker(sessions, runtime, clock, options=off).run(
        DOMAIN, second.id, mode="version"
    )
    assert skipped.skipped == "disabled"
    check = checker(sessions, runtime, clock, max_values=1)
    part = await check.run(DOMAIN, second.id, mode="version")
    assert part.work == 1
    assert part.resume is not None
    rest = await check.run(DOMAIN, second.id, mode="version", resume=part.resume)
    assert rest.work == 1
    assert rest.resume is None
    assert sorted(c.keys[0] for c in await candidates(sessions)) == ["S-1001", "S-1002"]
