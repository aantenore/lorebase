"""Outbox events written by the commit pipeline (design 6.6 step 8).

The audit entry of the same step goes through `lorebase_store.auth.AuditRepository`, which owns
the per-Domain hash chain.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Table, insert
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.models import metadata

OUTBOX: Table = metadata.tables["outbox"]


class OutboxRepository:
    """Events delivered after commit by a background worker."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(self, domain_id: str, event_type: str, payload: dict[str, Any]) -> int:
        result = await self._session.execute(
            insert(OUTBOX)
            .values(domain_id=domain_id, event_type=event_type, payload=payload)
            .returning(OUTBOX.c.id)
        )
        return int(result.scalar_one())
