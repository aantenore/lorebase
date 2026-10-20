"""Outbox events written by the commit pipeline (design 6.6 step 8) and read by the change
feed and the webhook worker.

The audit entry of the same step goes through `lorebase_store.auth.AuditRepository`, which owns
the per-Domain hash chain. Outbox ids increase with every event, so they serve as the sequence
numbers of the change feed (`Last-Event-ID`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Table, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.models import metadata

OUTBOX: Table = metadata.tables["outbox"]


@dataclass(frozen=True, slots=True)
class OutboxRow:
    id: int
    domain_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


def _row(row: Any) -> OutboxRow:
    return OutboxRow(row.id, row.domain_id, row.event_type, row.payload, row.created_at)


_COLUMNS = (
    OUTBOX.c.id,
    OUTBOX.c.domain_id,
    OUTBOX.c.event_type,
    OUTBOX.c.payload,
    OUTBOX.c.created_at,
)


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

    async def latest(self, domain_id: str) -> int:
        """Id of the newest event of the Domain (0 when there is none)."""
        found = await self._session.scalar(
            select(func.max(OUTBOX.c.id)).where(OUTBOX.c.domain_id == domain_id)
        )
        return int(found or 0)

    async def after(
        self,
        domain_id: str,
        after: int,
        *,
        event_types: Sequence[str],
        branch: str | None,
        limit: int,
    ) -> list[OutboxRow]:
        """Events of the Domain newer than `after`, oldest first; `branch` filters on the
        `branch` member of the payload."""
        query = (
            select(*_COLUMNS)
            .where(
                OUTBOX.c.domain_id == domain_id,
                OUTBOX.c.id > after,
                OUTBOX.c.event_type.in_(list(event_types)),
            )
            .order_by(OUTBOX.c.id)
            .limit(limit)
        )
        if branch is not None:
            query = query.where(OUTBOX.c.payload["branch"].astext == branch)
        return [_row(row) for row in await self._session.execute(query)]

    async def claim_undelivered(self, limit: int) -> list[OutboxRow]:
        """The oldest undelivered events of every Domain visible to the transaction, locked
        (skipping rows other workers hold)."""
        query = (
            select(*_COLUMNS)
            .where(OUTBOX.c.delivered_at.is_(None))
            .order_by(OUTBOX.c.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return [_row(row) for row in await self._session.execute(query)]

    async def mark_delivered(self, ids: Sequence[int], at: datetime) -> None:
        if ids:
            await self._session.execute(
                update(OUTBOX)
                .where(OUTBOX.c.id.in_(list(ids)))
                .values(delivered_at=at, attempts=OUTBOX.c.attempts + 1)
            )

    async def prune_delivered(self, before: datetime) -> int:
        """Delete events handed to delivery before `before`; the number deleted."""
        result = await self._session.execute(
            delete(OUTBOX).where(OUTBOX.c.delivered_at < before).returning(OUTBOX.c.id)
        )
        return len(result.all())
