"""Blob store interface for document bytes and its default Postgres backend (design 9.2, AC-24).

Blobs are content addressed: the reference names the backend and the SHA-256 of the bytes, so
storing the same bytes twice keeps one copy. The Postgres backend writes `document_blobs` in the
caller's transaction (row-level security applies); another backend (for example an
S3-compatible store) implements the same protocol and ignores the session.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

from sqlalchemy import Table, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.models import metadata

BLOBS: Table = metadata.tables["document_blobs"]
POSTGRES_SCHEME = "postgres"
TEXT_REF_KEY = "text_ref"
"""Document metadata member holding the blob reference of the parsed text (UTF-8)."""


class BlobNotFoundError(LookupError):
    """No blob under that reference."""


class BlobStore(Protocol):
    """Content-addressed storage of document bytes."""

    async def put(self, session: AsyncSession, domain_id: str, data: bytes) -> str:
        """Store `data` and return its reference (idempotent per content)."""
        ...

    async def get(self, session: AsyncSession, domain_id: str, ref: str) -> bytes:
        """The bytes stored under `ref`; raises `BlobNotFoundError`."""
        ...

    async def delete(self, session: AsyncSession, domain_id: str, ref: str) -> bool:
        """Delete the bytes stored under `ref` (document purge); false when there were none.
        The caller checks that no other document holds the reference."""
        ...


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


class PostgresBlobStore:
    """`BlobStore` keeping the bytes in `document_blobs` (references `postgres:<hex>`)."""

    async def put(self, session: AsyncSession, domain_id: str, data: bytes) -> str:
        digest = sha256(data)
        await session.execute(
            insert(BLOBS)
            .values(domain_id=domain_id, sha256=digest, content=data, size=len(data))
            .on_conflict_do_nothing(index_elements=["domain_id", "sha256"])
        )
        return f"{POSTGRES_SCHEME}:{digest.hex()}"

    @staticmethod
    def _key(ref: str) -> bytes:
        scheme, _, digest = ref.partition(":")
        if scheme != POSTGRES_SCHEME or not digest:
            raise BlobNotFoundError(f"not a Postgres blob reference: {ref!r}")
        try:
            return bytes.fromhex(digest)
        except ValueError:
            raise BlobNotFoundError(f"invalid blob reference: {ref!r}") from None

    async def delete(self, session: AsyncSession, domain_id: str, ref: str) -> bool:
        key = self._key(ref)
        deleted = (
            await session.execute(
                delete(BLOBS)
                .where(BLOBS.c.domain_id == domain_id, BLOBS.c.sha256 == key)
                .returning(BLOBS.c.sha256)
            )
        ).scalar_one_or_none()
        return deleted is not None

    async def get(self, session: AsyncSession, domain_id: str, ref: str) -> bytes:
        key = self._key(ref)
        content = (
            await session.execute(
                select(BLOBS.c.content).where(BLOBS.c.domain_id == domain_id, BLOBS.c.sha256 == key)
            )
        ).scalar_one_or_none()
        if content is None:
            raise BlobNotFoundError(f"no blob {ref!r} in Domain {domain_id!r}")
        return bytes(content)
