"""Document use cases behind `routes.documents`: intake and versions, listing, details,
retirement, purge and the parsed text (design 9.2 step 1, "Document versions and lifecycle").

Authorization and request parsing happen in the routes; this module stores documents, queues
their extraction and evidence checks, and writes the audit entries of new versions and purges
(the route's module doc describes the behavior).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import new_ulid
from lorebase_core.reconcile import CandidateStatus, CandidateType
from lorebase_keeper.context import StoreAccess, require_domain
from lorebase_keeper.errors import NOT_FOUND, ProblemError, ProblemType
from lorebase_store.auth import AuditRepository, NewAuditEntry
from lorebase_store.blobs import TEXT_REF_KEY, BlobNotFoundError, PostgresBlobStore, sha256
from lorebase_store.jobs import (
    DOCUMENT_VERSION_CHECK_JOB,
    EXTRACTION_JOB,
    cancel_queued_jobs,
    document_version_check_payload,
    enqueue_job,
    extraction_payload,
)
from lorebase_store.repositories import (
    DocumentRepository,
    DocumentRow,
    ExtractionJobRepository,
    ExtractionJobRow,
    ProposalItemRepository,
    SchemaSuggestionRepository,
)
from lorebase_store.repositories.documents import PURGED
from lorebase_store.repositories.reconciliation_candidates import (
    CandidateFilter,
    ReconciliationCandidateRepository,
)

DOCUMENT_PREFIX = "doc"
DOCUMENT_RETIRED = ProblemType("document-retired", 409, "Document is retired")
VERSION_ACTION = "document.version"
PURGE_ACTION = "document.purge"


def _not_found(document_id: str) -> ProblemError:
    return ProblemError(NOT_FOUND, f"Document {document_id!r} not found.")


# Intake -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NewDocument:
    """An accepted upload: checked name, bytes and media type, and the intake options."""

    filename: str
    data: bytes
    media_type: str
    metadata: dict[str, Any]
    source_key: str | None
    extract: bool | None
    """None: `keeper.documents.extract_on_upload`."""
    tiers: list[int] | None


@dataclass(frozen=True)
class Uploaded:
    row: DocumentRow
    created: bool
    sha256: bytes
    job_id: int | None
    check_job_id: int | None


async def upload(
    store: StoreAccess,
    domain_id: str,
    document: NewDocument,
    principal: str,
    *,
    extract_on_upload: bool,
) -> Uploaded:
    """Store `document` (a new version of its `source_key` when it has one) and queue its
    extraction, or the evidence check of a new version that is not extracted."""
    data, key = document.data, document.source_key
    digest = sha256(data)
    asked = document.extract
    extract = extract_on_upload if asked is None else asked
    job_id: int | None = None
    check_job: int | None = None
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        ref = await PostgresBlobStore().put(session, domain_id, data)
        repository = DocumentRepository(session, domain_id)
        candidate = DocumentRow(
            id=f"{DOCUMENT_PREFIX}_{new_ulid()}",
            filename=document.filename,
            media_type=document.media_type,
            sha256=digest,
            size=len(data),
            blob_ref=ref,
            created_by=principal,
            metadata=document.metadata,
            source_key=key,
        )
        replaced: DocumentRow | None = None
        if key is None:
            row, created = await repository.add(candidate)
        else:
            outcome = await repository.add_version(candidate)
            row, created, replaced = outcome.row, outcome.created, outcome.replaced
        if extract and not row.extractable:
            if asked:
                raise ProblemError(
                    DOCUMENT_RETIRED,
                    f"Document {row.id!r} is {row.status}: it is not extracted again.",
                )
            extract = False
        if extract:
            job_id = await enqueue_job(
                session,
                EXTRACTION_JOB,
                domain_id=domain_id,
                payload=extraction_payload([row.id], tiers=document.tiers, requested_by=principal),
                unique=True,
            )
        elif created and row.replaces is not None:
            check_job = await enqueue_job(
                session,
                DOCUMENT_VERSION_CHECK_JOB,
                domain_id=domain_id,
                payload=document_version_check_payload(row.id, mode="version"),
                unique=True,
            )
        if created and key is not None and row.version > 1:
            previous = replaced or (
                None if row.replaces is None else await repository.get(row.replaces)
            )
            await AuditRepository(session).append(
                NewAuditEntry(
                    actor=principal,
                    action=VERSION_ACTION,
                    domain_id=domain_id,
                    target=row.id,
                    details={
                        "source_key": key,
                        "version": row.version,
                        "replaces": row.replaces,
                        "size": row.size,
                        "sha256": digest.hex(),
                        "replaced_size": None if previous is None else previous.size,
                        "replaced_sha256": None if previous is None else previous.sha256.hex(),
                    },
                    at=store.clock(),
                )
            )
    return Uploaded(row, created, digest, job_id, check_job)


# Reads --------------------------------------------------------------------------------------


async def page(
    store: StoreAccess,
    domain_id: str,
    *,
    limit: int,
    after: tuple[datetime, str] | None,
    source_key: str | None,
) -> list[DocumentRow]:
    """Documents newest first; with `source_key`, the versions of that key."""
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        return await DocumentRepository(session, domain_id).page(
            limit=limit, after=after, source_key=source_key
        )


@dataclass(frozen=True)
class DocumentDetail:
    row: DocumentRow
    jobs: list[ExtractionJobRow]
    open_stale_candidates: int


async def detail(
    store: StoreAccess, domain_id: str, document_id: str, *, jobs_listed: int
) -> DocumentDetail:
    """The document, its latest extraction jobs and its open `stale` candidates."""
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        row = await DocumentRepository(session, domain_id).get(document_id)
        if row is None:
            raise _not_found(document_id)
        jobs = await ExtractionJobRepository(session, domain_id).of_document(
            document_id, limit=jobs_listed
        )
        stale = await ReconciliationCandidateRepository(session, domain_id).count(
            CandidateFilter(
                type=CandidateType.STALE,
                status=CandidateStatus.OPEN,
                source=f"document:{document_id}",
            )
        )
    return DocumentDetail(row, list(jobs), stale)


async def versions(
    store: StoreAccess, domain_id: str, document_id: str, *, limit: int, before: int | None
) -> list[DocumentRow]:
    """The versions of the document's `source_key` older than `before`, newest first; a
    keyless document is its only version."""
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        repository = DocumentRepository(session, domain_id)
        row = await repository.get(document_id)
        if row is None:
            raise _not_found(document_id)
        if row.source_key is None:
            return [] if before is not None else [row]
        return await repository.versions(row.source_key, limit=limit, before=before)


async def parsed_text_ref(store: StoreAccess, domain_id: str, document_id: str) -> tuple[str, str]:
    """The hash (hex) and blob reference of the document's parsed text; 404 until parsed."""
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        row = await DocumentRepository(session, domain_id).get(document_id)
    if row is None:
        raise _not_found(document_id)
    ref = row.metadata.get(TEXT_REF_KEY)
    if row.text_hash is None or not isinstance(ref, str):
        raise ProblemError(
            NOT_FOUND, f"The text of document {document_id!r} has not been parsed yet."
        )
    return row.text_hash.hex(), ref


async def parsed_text(store: StoreAccess, domain_id: str, document_id: str, ref: str) -> str:
    """The parsed text stored under `ref`."""
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        try:
            data = await PostgresBlobStore().get(session, domain_id, ref)
        except BlobNotFoundError:
            raise ProblemError(
                NOT_FOUND, f"The text of document {document_id!r} is not stored."
            ) from None
    return data.decode("utf-8")


# Lifecycle ----------------------------------------------------------------------------------


async def _retire(
    session: AsyncSession, domain_id: str, row: DocumentRow, principal: str, at: datetime
) -> tuple[DocumentRow, int | None]:
    """Retire `row` if it is not yet and queue its evidence check (module doc)."""
    retired = await DocumentRepository(session, domain_id).retire(row.id, by=principal, at=at)
    if retired is None:
        return row, None
    job = await enqueue_job(
        session,
        DOCUMENT_VERSION_CHECK_JOB,
        domain_id=domain_id,
        payload=document_version_check_payload(row.id, mode="retire"),
        unique=True,
    )
    return retired, job


async def retire(
    store: StoreAccess, domain_id: str, document_id: str, principal: str
) -> tuple[DocumentRow, int | None]:
    """Retire the document; the queued evidence check, None when it was retired before."""
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        row = await DocumentRepository(session, domain_id).lock(document_id)
        if row is None:
            raise _not_found(document_id)
        return await _retire(session, domain_id, row, principal, store.clock())


@dataclass(frozen=True)
class Purged:
    """What a purge did; `counts` lists, in order, the members of its answer and audit entry
    besides the document."""

    row: DocumentRow
    counts: Mapping[str, Any]


async def purge(
    store: StoreAccess,
    domain_id: str,
    document_id: str,
    principal: str,
    *,
    reason: str,
    erased_value: str,
) -> Purged:
    """Delete the document's bytes, text, chunks and quotes, keeping its provenance; retires
    it first if needed. Audited `document.purge` in the same transaction."""
    blobs = PostgresBlobStore()
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        repository = DocumentRepository(session, domain_id)
        # The row lock serialises the purge with extraction jobs writing this document's
        # text, chunks or items: they take it too and stop once it is retired or purged.
        row = await repository.lock(document_id)
        if row is None:
            raise _not_found(document_id)
        at = store.clock()
        already = row.status == PURGED
        job: int | None = None
        if not already:
            row, job = await _retire(session, domain_id, row, principal, at)
        extraction_jobs = ExtractionJobRepository(session, domain_id)
        queued = await cancel_queued_jobs(
            session,
            EXTRACTION_JOB,
            domain_id=domain_id,
            match={"documents": [document_id]},
            reason=f"document {document_id} purged",
        )
        cancelled = await extraction_jobs.cancel_of_document(document_id, at=at)
        jobs = await extraction_jobs.ids_of_document(document_id)
        items = await ProposalItemRepository(session, domain_id).erase_quotes(
            jobs, document_id, erased_value
        )
        candidates = await ReconciliationCandidateRepository(session, domain_id).erase_quotes(
            document_id, erased_value
        )
        suggestions = await SchemaSuggestionRepository(session, domain_id).erase_quotes(
            document_id, erased_value
        )
        content = await repository.purge_content(document_id)
        text_ref = row.metadata.get(TEXT_REF_KEY)
        text_deleted = False
        if (
            isinstance(text_ref, str)
            and text_ref != row.blob_ref
            and not await repository.shares_blob(document_id, text_ref)
        ):
            text_deleted = await blobs.delete(session, domain_id, text_ref)
        blob_deleted = False
        if not await repository.shares_blob(document_id, row.blob_ref):
            blob_deleted = await blobs.delete(session, domain_id, row.blob_ref)
        purged = await repository.mark_purged(document_id, by=principal, at=at)
        row = purged or row
        counts = {
            "job_id": job,
            "blob_deleted": blob_deleted,
            "text_deleted": text_deleted,
            "chunks": content.chunks,
            "cache_entries": content.cache_entries,
            "items": items,
            "candidates": candidates,
            "suggestions": suggestions,
        }
        await AuditRepository(session).append(
            NewAuditEntry(
                actor=principal,
                action=PURGE_ACTION,
                domain_id=domain_id,
                target=document_id,
                details={
                    "reason": reason,
                    "again": already,
                    "sha256": row.sha256.hex(),
                    "source_key": row.source_key,
                    "version": row.version,
                    "jobs_cancelled": queued + len(cancelled),
                    **counts,
                },
                at=at,
            )
        )
    return Purged(row, counts)
