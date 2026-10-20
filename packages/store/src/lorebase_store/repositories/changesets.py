"""Changesets: per-Revision element changes against the first parent (design 6.3)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.revision import ChangesetEntry
from lorebase_store.bulk import OnConflict, bulk_write
from lorebase_store.models import metadata
from lorebase_store.settings import WriteSettings

CHANGESETS: Table = metadata.tables["changesets"]


class ChangesetRepository:
    """Changeset rows of one Domain."""

    def __init__(self, session: AsyncSession, domain_id: str, *, writes: WriteSettings) -> None:
        self._session = session
        self._domain = domain_id
        self._writes = writes

    async def insert(self, revision_id: bytes, entries: Sequence[ChangesetEntry]) -> None:
        rows = [
            {
                "domain_id": self._domain,
                "revision_id": revision_id,
                "partition": entry.partition,
                "element_key": entry.key,
                "old_hash": entry.old_hash,
                "new_hash": entry.new_hash,
                "provenance": dict(entry.provenance) or None,
            }
            for entry in entries
        ]
        await bulk_write(
            self._session, CHANGESETS, rows, writes=self._writes, on_conflict=OnConflict.ERROR
        )

    async def entries(self, revision_id: bytes) -> list[ChangesetEntry]:
        """Entries of a Revision ordered by partition and element key."""
        result = await self._session.execute(
            select(
                CHANGESETS.c.partition,
                CHANGESETS.c.element_key,
                CHANGESETS.c.old_hash,
                CHANGESETS.c.new_hash,
                CHANGESETS.c.provenance,
            )
            .where(CHANGESETS.c.domain_id == self._domain, CHANGESETS.c.revision_id == revision_id)
            .order_by(CHANGESETS.c.partition, CHANGESETS.c.element_key)
        )
        return [
            ChangesetEntry(partition, key, old, new, provenance or {})
            for partition, key, old, new, provenance in result
        ]
