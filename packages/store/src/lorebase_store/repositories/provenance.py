"""Provenance entries: where values came from (design 5.11, AC-08)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import ProvenanceEntry
from lorebase_store.bulk import bulk_write, chunks
from lorebase_store.models import metadata
from lorebase_store.settings import WriteSettings

PROVENANCE: Table = metadata.tables["provenance"]


@dataclass(frozen=True, slots=True)
class ProvenanceRow:
    id: str
    origin_type: str
    origin_ref: dict[str, Any]
    actor: str
    at: datetime

    @classmethod
    def from_entry(cls, entry: ProvenanceEntry) -> ProvenanceRow:
        """Row of a core provenance entry; `origin_ref` holds the whole origin document."""
        return cls(
            id=entry.id,
            origin_type=entry.origin.type,
            origin_ref=entry.origin.content(),
            actor=entry.actor,
            at=datetime.fromisoformat(entry.at),
        )


class ProvenanceRepository:
    """Provenance rows of one Domain. Entries are immutable: re-inserting one is a no-op."""

    def __init__(self, session: AsyncSession, domain_id: str, *, writes: WriteSettings) -> None:
        self._session = session
        self._domain = domain_id
        self._writes = writes

    async def insert_many(self, rows: Iterable[ProvenanceRow]) -> None:
        values = [
            {
                "domain_id": self._domain,
                "id": row.id,
                "origin_type": row.origin_type,
                "origin_ref": row.origin_ref,
                "actor": row.actor,
                "at": row.at,
            }
            for row in {row.id: row for row in rows}.values()
        ]
        await bulk_write(self._session, PROVENANCE, values, writes=self._writes)

    async def get_many(self, ids: Iterable[str]) -> dict[str, ProvenanceRow]:
        found: dict[str, ProvenanceRow] = {}
        for chunk in chunks(list(dict.fromkeys(ids)), self._writes.insert_batch_rows):
            result = await self._session.execute(
                select(
                    PROVENANCE.c.id,
                    PROVENANCE.c.origin_type,
                    PROVENANCE.c.origin_ref,
                    PROVENANCE.c.actor,
                    PROVENANCE.c.at,
                ).where(PROVENANCE.c.domain_id == self._domain, PROVENANCE.c.id.in_(chunk))
            )
            for row in result.mappings():
                found[row["id"]] = ProvenanceRow(**row)
        return found
