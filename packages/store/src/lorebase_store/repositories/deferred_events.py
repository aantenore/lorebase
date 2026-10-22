"""Operation events deferred to the merge of a Proposal (design 10.5).

An execution committed on a branch whose Proposal is its review (agent writes) has not
changed its target yet, so its `operation.<side effect>` events wait in
`operation_deferred_events`, keyed by branch and by the head after the execution. The merge
of a Proposal from that branch writes to the outbox, in its transaction, the events whose
Revision the merged source head contains, and deletes them: each is written once, and two
merges racing over the same rows serialize on their row locks. Events of executions the merged
head does not contain (committed after the merge was computed) wait for the next merge;
deleting the branch drops what is left, and so does `expire` after `store.outbox.max_age_s`
(a Proposal left open that long), like the outbox does with events nobody handed off.
Released events are counted in `lorebase.operations.events` when the merge commits.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import Table, delete, event, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from lorebase_store.models import metadata
from lorebase_store.repositories.events import OutboxRepository
from lorebase_store.repositories.revisions import RevisionRepository
from lorebase_store.telemetry import OPERATION_EVENTS

DEFERRED: Table = metadata.tables["operation_deferred_events"]


class DeferredEventRepository:
    """Deferred Operation events of one Domain."""

    def __init__(self, session: AsyncSession, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id

    async def defer(
        self,
        *,
        branch: str,
        revision: bytes,
        invocation_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        """Keep an event until a merge brings `revision` of `branch` into a target."""
        result = await self._session.execute(
            insert(DEFERRED)
            .values(
                domain_id=self._domain,
                branch=branch,
                revision_id=revision,
                invocation_id=invocation_id,
                event_type=event_type,
                payload=payload,
            )
            .returning(DEFERRED.c.id)
        )
        return int(result.scalar_one())

    async def release(
        self, branch: str, merged_head: bytes, fields: Mapping[str, Any]
    ) -> list[int]:
        """Write to the outbox the events of `branch` whose Revision `merged_head` contains,
        their payloads updated with `fields`, and delete them; the outbox ids, in the order
        the events were deferred."""
        rows = (
            await self._session.execute(
                select(
                    DEFERRED.c.id, DEFERRED.c.revision_id, DEFERRED.c.event_type, DEFERRED.c.payload
                )
                .where(DEFERRED.c.domain_id == self._domain, DEFERRED.c.branch == branch)
                .order_by(DEFERRED.c.id)
                .with_for_update()
            )
        ).all()
        if not rows:
            return []
        contained = await RevisionRepository(self._session, self._domain).contained(
            merged_head, {row.revision_id for row in rows}
        )
        released = [row for row in rows if row.revision_id in contained]
        if not released:
            return []
        outbox = OutboxRepository(self._session)
        ids = [
            await outbox.enqueue(self._domain, row.event_type, {**row.payload, **fields})
            for row in released
        ]
        await self._session.execute(
            delete(DEFERRED).where(DEFERRED.c.id.in_([row.id for row in released]))
        )
        _count_on_commit(self._session, len(ids))
        return ids

    async def discard(self, branch: str) -> int:
        """Drop the events still deferred on `branch` (it is being deleted)."""
        result = await self._session.execute(
            delete(DEFERRED)
            .where(DEFERRED.c.domain_id == self._domain, DEFERRED.c.branch == branch)
            .returning(DEFERRED.c.id)
        )
        return len(result.all())


async def expire(session: AsyncSession, before: datetime, *, limit: int) -> int:
    """Delete up to `limit` deferred events created before `before`, in every Domain the
    transaction sees; the number deleted."""
    oldest = (
        select(DEFERRED.c.id)
        .where(DEFERRED.c.created_at < before)
        .order_by(DEFERRED.c.id)
        .limit(limit)
    )
    result = await session.execute(
        delete(DEFERRED).where(DEFERRED.c.id.in_(oldest)).returning(DEFERRED.c.id)
    )
    return len(result.all())


def _count_on_commit(session: AsyncSession, events: int) -> None:
    """Count `events` in `lorebase.operations.events` once the session's transaction commits
    (nothing when it rolls back)."""
    if not events:
        return
    settled = False

    def committed(_: Session) -> None:
        nonlocal settled
        if not settled:
            OPERATION_EVENTS.add(events)
        settled = True

    def rolled_back(_: Session) -> None:
        nonlocal settled
        settled = True

    event.listen(session.sync_session, "after_commit", committed, once=True)
    event.listen(session.sync_session, "after_rollback", rolled_back, once=True)
