"""Outbox events written in commit transactions (design 6.6 step 8, 10.5) and read by the
change feed and the consumers of the outbox.

The audit entry of the same step goes through `lorebase_store.auth.AuditRepository`, which owns
the per-Domain hash chain. Outbox ids increase with every event, so they serve as the sequence
numbers of the change feed (`Last-Event-ID`).

Each consumer (`outbox_consumers`) owns its delivery state: the events it has not handed off
yet are its `outbox_pending` rows, added by a trigger in the transaction writing the event, so
an event committed late (its transaction began before others that committed first) is never
skipped the way a position in the id sequence could skip it. A consumer claims its pending
rows (`claim`, skipping rows another worker of the same consumer holds) and deletes them with
`acknowledge` in the transaction that hands the events off; a failure before that leaves them
pending (at-least-once). Several consumers never take events from one another.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Table, delete, exists, func, insert, select
from sqlalchemy.dialects.postgresql import insert as upsert
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.models import metadata

OUTBOX: Table = metadata.tables["outbox"]
OUTBOX_CONSUMERS: Table = metadata.tables["outbox_consumers"]
OUTBOX_PENDING: Table = metadata.tables["outbox_pending"]


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

    async def claim(self, consumer: str, limit: int) -> list[OutboxRow]:
        """The oldest events pending for `consumer`, in every Domain visible to the
        transaction; their pending rows stay locked until it ends (other workers of the
        consumer skip them)."""
        p = OUTBOX_PENDING
        claimed = (
            select(p.c.event_id)
            .where(p.c.consumer == consumer)
            .order_by(p.c.event_id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("claimed")
        )
        query = (
            select(*_COLUMNS).join(claimed, claimed.c.event_id == OUTBOX.c.id).order_by(OUTBOX.c.id)
        )
        return [_row(row) for row in await self._session.execute(query)]

    async def acknowledge(self, consumer: str, ids: Sequence[int]) -> None:
        """`consumer` handed these events off: they are no longer pending for it."""
        if ids:
            await self._session.execute(
                delete(OUTBOX_PENDING).where(
                    OUTBOX_PENDING.c.consumer == consumer,
                    OUTBOX_PENDING.c.event_id.in_(list(ids)),
                )
            )

    async def pending(self) -> dict[str, int]:
        """Events each registered consumer has not handed off yet (its backlog)."""
        p, c = OUTBOX_PENDING, OUTBOX_CONSUMERS
        result = await self._session.execute(
            select(c.c.name, func.count(p.c.event_id))
            .select_from(c.outerjoin(p, p.c.consumer == c.c.name))
            .group_by(c.c.name)
        )
        return {name: int(count) for name, count in result}

    async def prune(
        self, *, before: datetime, expire_before: datetime, limit: int
    ) -> tuple[int, int]:
        """Delete up to `limit` events created before `expire_before` whatever is pending, then
        up to `limit` created before `before` that no consumer has pending, oldest first; the
        numbers deleted (handed off, expired). Bounded, so a large backlog is deleted over
        several calls instead of in one long transaction."""
        expired_ids = (
            select(OUTBOX.c.id)
            .where(OUTBOX.c.created_at < expire_before)
            .order_by(OUTBOX.c.id)
            .limit(limit)
        )
        expired = await self._session.execute(
            delete(OUTBOX).where(OUTBOX.c.id.in_(expired_ids)).returning(OUTBOX.c.id)
        )
        handed_ids = (
            select(OUTBOX.c.id)
            .where(
                OUTBOX.c.created_at < before,
                ~exists().where(OUTBOX_PENDING.c.event_id == OUTBOX.c.id),
            )
            .order_by(OUTBOX.c.id)
            .limit(limit)
        )
        handed = await self._session.execute(
            delete(OUTBOX).where(OUTBOX.c.id.in_(handed_ids)).returning(OUTBOX.c.id)
        )
        return len(handed.all()), len(expired.all())


async def register_consumer(session: AsyncSession, name: str) -> None:
    """Make `name` a consumer of the outbox: events written from now on are pending for it."""
    await session.execute(upsert(OUTBOX_CONSUMERS).values(name=name).on_conflict_do_nothing())


async def drop_consumer(session: AsyncSession, name: str) -> bool:
    """Stop keeping events for `name` (its pending rows go too); false when unknown."""
    result = await session.execute(
        delete(OUTBOX_CONSUMERS)
        .where(OUTBOX_CONSUMERS.c.name == name)
        .returning(OUTBOX_CONSUMERS.c.name)
    )
    return result.first() is not None
