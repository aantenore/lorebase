"""Documents, their chunks and extraction jobs (design 7.4, 9.2)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any

from sqlalchemy import Table, and_, delete, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.models import metadata
from lorebase_store.models.enums import RunStatus

DOCUMENTS: Table = metadata.tables["documents"]
CHUNKS: Table = metadata.tables["document_chunks"]
EXTRACTION_JOBS: Table = metadata.tables["extraction_jobs"]
CACHE: Table = metadata.tables["extraction_cache"]


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

    async def add(self, row: DocumentRow) -> tuple[DocumentRow, bool]:
        """Insert the document, or return the stored one with the same SHA-256; the flag tells
        whether a row was inserted."""
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
        }
        if row.created_at is not None:
            values["created_at"] = row.created_at
        inserted = (
            await self._session.execute(
                pg_insert(DOCUMENTS)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["domain_id", "sha256"])
                .returning(DOCUMENTS.c.id)
            )
        ).scalar_one_or_none()
        stored = await self.by_sha256(row.sha256)
        assert stored is not None
        return stored, inserted is not None

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

    async def by_sha256(self, sha256: bytes) -> DocumentRow | None:
        return await self._one(DOCUMENTS.c.sha256 == sha256)

    async def page(
        self, *, limit: int, after: tuple[datetime, str] | None = None
    ) -> list[DocumentRow]:
        """Documents, newest first, after the keyset `(created_at, id)`."""
        table = DOCUMENTS
        query = select(table).where(table.c.domain_id == self._domain)
        if after is not None:
            at, last_id = after
            query = query.where(
                or_(table.c.created_at < at, and_(table.c.created_at == at, table.c.id < last_id))
            )
        query = query.order_by(table.c.created_at.desc(), table.c.id.desc()).limit(limit)
        return [_document(row) for row in (await self._session.execute(query)).mappings()]

    async def set_text(
        self, document_id: str, *, text_hash: bytes, metadata: dict[str, Any]
    ) -> None:
        """Record the hash of the parsed text and merge `metadata` into the stored one."""
        await self._session.execute(
            update(DOCUMENTS)
            .where(DOCUMENTS.c.domain_id == self._domain, DOCUMENTS.c.id == document_id)
            .values(text_hash=text_hash, metadata=DOCUMENTS.c.metadata.op("||")(metadata))
        )


class ChunkRepository:
    """Chunks of the documents of one Domain."""

    def __init__(self, session: AsyncSession, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id

    async def replace(self, document_id: str, chunks: Sequence[ChunkRow]) -> None:
        """Store `chunks` as the chunks of the document, dropping the previous ones."""
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

    async def put(self, key: bytes, *, model: str, output: Any, usage: dict[str, Any]) -> None:
        await self._session.execute(
            pg_insert(CACHE)
            .values(cache_key=key, model=model, output=output, usage=usage)
            .on_conflict_do_nothing(index_elements=["cache_key"])
        )
