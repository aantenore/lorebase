"""Documents, their chunks and extraction jobs (design 7.4, 9.2).

Document identity (design 9.2 "Document versions and lifecycle"): a keyless document is one
row per content hash (`add` returns the stored one for the same bytes); a keyed document is a
version of its `source_key` (`add_version`): bytes equal to the active version return it, other
bytes become version n + 1 replacing it, the previous one `superseded`. `retire` and
`mark_purged` move a document along its lifecycle; `purge_content` deletes its chunks and the
cached answers of chunk hashes no other document of the Domain holds.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any

from sqlalchemy import Table, and_, delete, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.blobs import TEXT_REF_KEY
from lorebase_store.models import metadata
from lorebase_store.models.enums import RunStatus

DOCUMENTS: Table = metadata.tables["documents"]
CHUNKS: Table = metadata.tables["document_chunks"]
EXTRACTION_JOBS: Table = metadata.tables["extraction_jobs"]
CACHE: Table = metadata.tables["extraction_cache"]


ACTIVE = "active"
SUPERSEDED = "superseded"
RETIRED = "retired"
PURGED = "purged"
DOCUMENT_STATUSES = (ACTIVE, SUPERSEDED, RETIRED, PURGED)
NOT_EXTRACTED = frozenset({RETIRED, PURGED})
"""Statuses of documents that are never extracted again."""

_KEYLESS = text("source_key IS NULL AND status <> 'purged'")


@dataclass(frozen=True, slots=True)
class DocumentRow:
    id: str
    filename: str
    media_type: str
    sha256: bytes
    size: int
    blob_ref: str
    created_by: str
    metadata: dict[str, Any] = field(default_factory=dict)
    text_hash: bytes | None = None
    created_at: datetime | None = None
    source_key: str | None = None
    version: int = 1
    replaces: str | None = None
    status: str = ACTIVE
    retired_at: datetime | None = None
    retired_by: str | None = None
    purged_at: datetime | None = None
    purged_by: str | None = None

    @property
    def extractable(self) -> bool:
        """Whether the document may be extracted (not retired or purged)."""
        return self.status not in NOT_EXTRACTED


@dataclass(frozen=True, slots=True)
class VersionOutcome:
    """What `add_version` did: the document stored for the bytes, whether it was inserted, and
    the version it replaced (`None` for the first version of a key or a no-op)."""

    row: DocumentRow
    created: bool
    replaced: DocumentRow | None = None


@dataclass(frozen=True, slots=True)
class PurgedContent:
    """What `purge_content` deleted."""

    chunks: int
    cache_entries: int


@dataclass(frozen=True, slots=True)
class ChunkRow:
    """A chunk of a document's text; offsets index the parsed text (end exclusive)."""

    seq: int
    start_offset: int
    end_offset: int
    text: str
    chunk_hash: bytes


@dataclass(frozen=True, slots=True)
class ExtractionJobRow:
    id: str
    documents: list[str]
    schema_hash: bytes
    model: str
    prompt_version: str
    status: RunStatus
    budget: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    proposal_id: str | None = None
    created_at: datetime | None = None
    created_by: str | None = None
    branch: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None


_JOB_FIELDS = tuple(f.name for f in fields(ExtractionJobRow))
_JOB_UPDATES = frozenset(
    {"status", "model", "usage", "proposal_id", "finished_at", "stats", "error", "schema_hash"}
)


def _document(found: Any) -> DocumentRow:
    values = {k: v for k, v in found.items() if k not in ("domain_id", "metadata")}
    return DocumentRow(**values, metadata=found["metadata"] or {})


def _job(found: Any) -> ExtractionJobRow:
    return ExtractionJobRow(**{name: found[name] for name in _JOB_FIELDS})


class DocumentRepository:
    """Documents of one Domain, in the caller's transaction (after `scope_to_domain`)."""

    def __init__(self, session: AsyncSession, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id

    def _values(self, row: DocumentRow) -> dict[str, Any]:
        values: dict[str, Any] = {
            "domain_id": self._domain,
            "id": row.id,
            "filename": row.filename,
            "media_type": row.media_type,
            "sha256": row.sha256,
            "size": row.size,
            "blob_ref": row.blob_ref,
            "text_hash": row.text_hash,
            "metadata": row.metadata,
            "created_by": row.created_by,
            "source_key": row.source_key,
            "version": row.version,
            "replaces": row.replaces,
            "status": row.status,
        }
        if row.created_at is not None:
            values["created_at"] = row.created_at
        return values

    async def add(self, row: DocumentRow) -> tuple[DocumentRow, bool]:
        """Insert a keyless document, or return the stored one with the same SHA-256 (purged
        ones aside); the flag tells whether a row was inserted."""
        if row.source_key is not None:
            raise ValueError("a keyed document is added with add_version")
        inserted = (
            await self._session.execute(
                pg_insert(DOCUMENTS)
                .values(**self._values(row))
                .on_conflict_do_nothing(
                    index_elements=["domain_id", "sha256"], index_where=_KEYLESS
                )
                .returning(DOCUMENTS.c.id)
            )
        ).scalar_one_or_none()
        stored = await self.by_sha256(row.sha256)
        assert stored is not None
        return stored, inserted is not None

    async def add_version(self, row: DocumentRow) -> VersionOutcome:
        """Store `row` (keyed) as the next version of its `source_key` (module doc). Uploads
        of one key are serialized by a transaction-level advisory lock."""
        key = row.source_key
        if key is None:
            raise ValueError("add_version needs a source_key")
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(f"{self._domain}\x1f{key}", 0)))
        )
        current = await self.current_version(key)
        if current is not None and current.status == ACTIVE and current.sha256 == row.sha256:
            return VersionOutcome(current, created=False)
        latest = (
            await self._session.execute(
                select(func.max(DOCUMENTS.c.version)).where(
                    DOCUMENTS.c.domain_id == self._domain, DOCUMENTS.c.source_key == key
                )
            )
        ).scalar_one_or_none()
        replaced = current if current is not None and current.status == ACTIVE else None
        if replaced is not None:
            await self._session.execute(
                update(DOCUMENTS)
                .where(DOCUMENTS.c.domain_id == self._domain, DOCUMENTS.c.id == replaced.id)
                .values(status=SUPERSEDED)
            )
        stored = DocumentRow(
            **{
                **{f.name: getattr(row, f.name) for f in fields(DocumentRow)},
                "version": (latest or 0) + 1,
                "replaces": None if current is None else current.id,
                "status": ACTIVE,
            }
        )
        await self._session.execute(insert(DOCUMENTS).values(**self._values(stored)))
        found = await self.get(stored.id)
        assert found is not None
        return VersionOutcome(found, created=True, replaced=replaced)

    async def current_version(self, source_key: str) -> DocumentRow | None:
        """The newest version of a key, whatever its status."""
        found = (
            (
                await self._session.execute(
                    select(DOCUMENTS)
                    .where(
                        DOCUMENTS.c.domain_id == self._domain, DOCUMENTS.c.source_key == source_key
                    )
                    .order_by(DOCUMENTS.c.version.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if found is None else _document(found)

    async def versions(
        self, source_key: str, *, limit: int, before: int | None = None
    ) -> list[DocumentRow]:
        """Versions of a key, newest first, below version `before`."""
        query = select(DOCUMENTS).where(
            DOCUMENTS.c.domain_id == self._domain, DOCUMENTS.c.source_key == source_key
        )
        if before is not None:
            query = query.where(DOCUMENTS.c.version < before)
        query = query.order_by(DOCUMENTS.c.version.desc()).limit(limit)
        return [_document(row) for row in (await self._session.execute(query)).mappings()]

    async def retire(self, document_id: str, *, by: str, at: datetime) -> DocumentRow | None:
        """Mark an active or superseded document retired; `None` when it was not found or
        already retired or purged."""
        found = (
            await self._session.execute(
                update(DOCUMENTS)
                .where(
                    DOCUMENTS.c.domain_id == self._domain,
                    DOCUMENTS.c.id == document_id,
                    DOCUMENTS.c.status.in_([ACTIVE, SUPERSEDED]),
                )
                .values(status=RETIRED, retired_at=at, retired_by=by)
                .returning(DOCUMENTS.c.id)
            )
        ).scalar_one_or_none()
        return None if found is None else await self.get(document_id)

    async def mark_purged(self, document_id: str, *, by: str, at: datetime) -> DocumentRow | None:
        """Mark a document purged: its text hash and text reference are dropped (the bytes and
        text are deleted by the caller); `None` when it was already purged."""
        found = (
            await self._session.execute(
                update(DOCUMENTS)
                .where(
                    DOCUMENTS.c.domain_id == self._domain,
                    DOCUMENTS.c.id == document_id,
                    DOCUMENTS.c.status != PURGED,
                )
                .values(
                    status=PURGED,
                    purged_at=at,
                    purged_by=by,
                    text_hash=None,
                    metadata=DOCUMENTS.c.metadata.op("-")(TEXT_REF_KEY),
                )
                .returning(DOCUMENTS.c.id)
            )
        ).scalar_one_or_none()
        return None if found is None else await self.get(document_id)

    async def shares_blob(self, document_id: str, ref: str) -> bool:
        """Whether a document other than `document_id`, not purged, holds `ref` as its bytes or
        its parsed text."""
        found = (
            await self._session.execute(
                select(DOCUMENTS.c.id)
                .where(
                    DOCUMENTS.c.domain_id == self._domain,
                    DOCUMENTS.c.id != document_id,
                    DOCUMENTS.c.status != PURGED,
                    or_(
                        DOCUMENTS.c.blob_ref == ref,
                        DOCUMENTS.c.metadata[TEXT_REF_KEY].astext == ref,
                    ),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return found is not None

    async def purge_content(self, document_id: str) -> PurgedContent:
        """Delete the chunks of a document and the cached answers of the chunk hashes no other
        document of the Domain holds."""
        hashes = list(
            (
                await self._session.execute(
                    delete(CHUNKS)
                    .where(CHUNKS.c.domain_id == self._domain, CHUNKS.c.document_id == document_id)
                    .returning(CHUNKS.c.chunk_hash)
                )
            ).scalars()
        )
        if not hashes:
            return PurgedContent(chunks=0, cache_entries=0)
        kept = set(
            (
                await self._session.execute(
                    select(CHUNKS.c.chunk_hash).where(
                        CHUNKS.c.domain_id == self._domain, CHUNKS.c.chunk_hash.in_(set(hashes))
                    )
                )
            ).scalars()
        )
        gone = sorted({bytes(h) for h in hashes} - {bytes(h) for h in kept})
        removed = 0
        if gone:
            removed = len(
                list(
                    (
                        await self._session.execute(
                            delete(CACHE)
                            .where(CACHE.c.chunk_hash.in_(gone))
                            .returning(CACHE.c.cache_key)
                        )
                    ).scalars()
                )
            )
        return PurgedContent(chunks=len(hashes), cache_entries=removed)

    async def _one(self, condition: Any) -> DocumentRow | None:
        found = (
            (
                await self._session.execute(
                    select(DOCUMENTS).where(DOCUMENTS.c.domain_id == self._domain, condition)
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if found is None else _document(found)

    async def get(self, document_id: str) -> DocumentRow | None:
        return await self._one(DOCUMENTS.c.id == document_id)

    async def lock(self, document_id: str) -> DocumentRow | None:
        """The document, its row locked until the end of the transaction (`FOR UPDATE`).
        Extraction writes, retirement and purge take this lock, so a purge never interleaves
        with a job writing the document's text, chunks or items."""
        found = (
            (
                await self._session.execute(
                    select(DOCUMENTS)
                    .where(DOCUMENTS.c.domain_id == self._domain, DOCUMENTS.c.id == document_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if found is None else _document(found)

    async def by_sha256(self, sha256: bytes) -> DocumentRow | None:
        """The keyless document holding these bytes (purged ones aside)."""
        return await self._one(
            and_(
                DOCUMENTS.c.sha256 == sha256,
                DOCUMENTS.c.source_key.is_(None),
                DOCUMENTS.c.status != PURGED,
            )
        )

    async def page(
        self,
        *,
        limit: int,
        after: tuple[datetime, str] | None = None,
        source_key: str | None = None,
    ) -> list[DocumentRow]:
        """Documents, newest first, after the keyset `(created_at, id)`; only the versions of
        `source_key` when given."""
        table = DOCUMENTS
        query = select(table).where(table.c.domain_id == self._domain)
        if source_key is not None:
            query = query.where(table.c.source_key == source_key)
        if after is not None:
            at, last_id = after
            query = query.where(
                or_(table.c.created_at < at, and_(table.c.created_at == at, table.c.id < last_id))
            )
        query = query.order_by(table.c.created_at.desc(), table.c.id.desc()).limit(limit)
        return [_document(row) for row in (await self._session.execute(query)).mappings()]

    async def set_text(
        self, document_id: str, *, text_hash: bytes, metadata: dict[str, Any]
    ) -> bool:
        """Record the hash of the parsed text and merge `metadata` into the stored one; a
        retired or purged document is left as it is (`False`)."""
        found = (
            await self._session.execute(
                update(DOCUMENTS)
                .where(
                    DOCUMENTS.c.domain_id == self._domain,
                    DOCUMENTS.c.id == document_id,
                    DOCUMENTS.c.status.not_in(NOT_EXTRACTED),
                )
                .values(text_hash=text_hash, metadata=DOCUMENTS.c.metadata.op("||")(metadata))
                .returning(DOCUMENTS.c.id)
            )
        ).scalar_one_or_none()
        return found is not None


class ChunkRepository:
    """Chunks of the documents of one Domain."""

    def __init__(self, session: AsyncSession, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id

    async def replace(self, document_id: str, chunks: Sequence[ChunkRow]) -> bool:
        """Store `chunks` as the chunks of the document, dropping the previous ones; nothing is
        written for a retired or purged document (`False`). The document row is share-locked,
        so a purge waits for this transaction."""
        extractable = (
            await self._session.execute(
                select(DOCUMENTS.c.id)
                .where(
                    DOCUMENTS.c.domain_id == self._domain,
                    DOCUMENTS.c.id == document_id,
                    DOCUMENTS.c.status.not_in(NOT_EXTRACTED),
                )
                .with_for_update(read=True)
            )
        ).scalar_one_or_none()
        if extractable is None:
            return False
        await self._session.execute(
            delete(CHUNKS).where(
                CHUNKS.c.domain_id == self._domain, CHUNKS.c.document_id == document_id
            )
        )
        if chunks:
            await self._session.execute(
                insert(CHUNKS),
                [
                    {
                        "domain_id": self._domain,
                        "document_id": document_id,
                        "seq": chunk.seq,
                        "start_offset": chunk.start_offset,
                        "end_offset": chunk.end_offset,
                        "text": chunk.text,
                        "chunk_hash": chunk.chunk_hash,
                    }
                    for chunk in chunks
                ],
            )
        return True

    async def held(self, chunk_hash: bytes) -> bool:
        """Whether a chunk with this hash is stored in the Domain, share-locking it, so a
        purge deleting it waits for the caller's transaction (or runs first and leaves
        nothing to find)."""
        found = (
            await self._session.execute(
                select(CHUNKS.c.seq)
                .where(CHUNKS.c.domain_id == self._domain, CHUNKS.c.chunk_hash == chunk_hash)
                .limit(1)
                .with_for_update(read=True)
            )
        ).scalar_one_or_none()
        return found is not None

    async def of(self, document_id: str) -> list[ChunkRow]:
        result = await self._session.execute(
            select(CHUNKS)
            .where(CHUNKS.c.domain_id == self._domain, CHUNKS.c.document_id == document_id)
            .order_by(CHUNKS.c.seq)
        )
        return [
            ChunkRow(
                seq=row["seq"],
                start_offset=row["start_offset"],
                end_offset=row["end_offset"],
                text=row["text"],
                chunk_hash=row["chunk_hash"],
            )
            for row in result.mappings()
        ]


class ExtractionJobRepository:
    """Extraction jobs of one Domain."""

    def __init__(self, session: AsyncSession, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id

    async def insert(self, row: ExtractionJobRow) -> None:
        values = {
            name: getattr(row, name)
            for name in _JOB_FIELDS
            if not (name == "created_at" and row.created_at is None)
        }
        await self._session.execute(
            insert(EXTRACTION_JOBS).values(domain_id=self._domain, **values)
        )

    async def get(self, job_id: str) -> ExtractionJobRow | None:
        found = (
            (
                await self._session.execute(
                    select(EXTRACTION_JOBS).where(
                        EXTRACTION_JOBS.c.domain_id == self._domain,
                        EXTRACTION_JOBS.c.id == job_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if found is None else _job(found)

    async def update(self, job_id: str, **values: Any) -> None:
        """Set columns of a job: `status`, `model`, `usage`, `proposal_id`, `finished_at`,
        `stats`, `error`, `schema_hash`."""
        unknown = values.keys() - _JOB_UPDATES
        if unknown:
            raise ValueError(f"cannot update {sorted(unknown)}")
        await self._session.execute(
            update(EXTRACTION_JOBS)
            .where(EXTRACTION_JOBS.c.domain_id == self._domain, EXTRACTION_JOBS.c.id == job_id)
            .values(**values)
        )

    async def cancel_of_document(self, document_id: str, *, at: datetime) -> list[str]:
        """Mark the queued and running jobs that read the document `cancelled`; their ids."""
        table = EXTRACTION_JOBS
        result = await self._session.execute(
            update(table)
            .where(
                table.c.domain_id == self._domain,
                table.c.documents.contains([document_id]),
                table.c.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]),
            )
            .values(status=RunStatus.CANCELLED, finished_at=at)
            .returning(table.c.id)
        )
        return sorted(str(i) for i in result.scalars())

    async def of_document(self, document_id: str, *, limit: int) -> list[ExtractionJobRow]:
        """Jobs that read the document, newest first."""
        table = EXTRACTION_JOBS
        query = (
            select(table)
            .where(table.c.domain_id == self._domain, table.c.documents.contains([document_id]))
            .order_by(table.c.created_at.desc(), table.c.id.desc())
            .limit(limit)
        )
        return [_job(row) for row in (await self._session.execute(query)).mappings()]

    async def ids_of_document(self, document_id: str) -> list[str]:
        """Ids of every job that read the document."""
        table = EXTRACTION_JOBS
        query = select(table.c.id).where(
            table.c.domain_id == self._domain, table.c.documents.contains([document_id])
        )
        return [str(i) for i in (await self._session.execute(query.order_by(table.c.id))).scalars()]

    async def by_proposal(self, proposal_id: str) -> ExtractionJobRow | None:
        found = (
            (
                await self._session.execute(
                    select(EXTRACTION_JOBS)
                    .where(
                        EXTRACTION_JOBS.c.domain_id == self._domain,
                        EXTRACTION_JOBS.c.proposal_id == proposal_id,
                    )
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if found is None else _job(found)


class ExtractionCacheRepository:
    """Model answers keyed by their inputs; installation-wide (the key covers the chunk, the
    schema, the model and the prompt version)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: bytes) -> tuple[Any, dict[str, Any], str] | None:
        """`(output, usage, model)` stored under `key`."""
        found = (
            await self._session.execute(
                select(CACHE.c.output, CACHE.c.usage, CACHE.c.model).where(CACHE.c.cache_key == key)
            )
        ).one_or_none()
        return None if found is None else (found[0], found[1], found[2])

    async def put(
        self,
        key: bytes,
        *,
        model: str,
        output: Any,
        usage: dict[str, Any],
        chunk_hash: bytes | None = None,
    ) -> None:
        """Store an answer; `chunk_hash` names the chunk it was computed from (purge)."""
        await self._session.execute(
            pg_insert(CACHE)
            .values(cache_key=key, model=model, output=output, usage=usage, chunk_hash=chunk_hash)
            .on_conflict_do_nothing(index_elements=["cache_key"])
        )
