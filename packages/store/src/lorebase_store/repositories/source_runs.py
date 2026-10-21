"""Synchronization runs of structured Sources (design 7.4, 9.1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Table, and_, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.models import metadata
from lorebase_store.models.enums import RunStatus

SOURCE_RUNS: Table = metadata.tables["source_runs"]


@dataclass(frozen=True, slots=True)
class SourceRunRow:
    id: str
    source_id: str
    status: RunStatus
    started_at: datetime | None
    finished_at: datetime | None
    stats: dict[str, Any]
    revision_id: bytes | None = None
    proposal_id: str | None = None
    error: dict[str, Any] | None = None


def _row(found: Any) -> SourceRunRow:
    return SourceRunRow(**{k: v for k, v in found.items() if k != "domain_id"})


class SourceRunRepository:
    """Source runs of one Domain, in the caller's transaction (after `scope_to_domain`)."""

    def __init__(self, session: AsyncSession, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id

    async def insert(self, row: SourceRunRow) -> None:
        await self._session.execute(
            insert(SOURCE_RUNS).values(
                domain_id=self._domain,
                id=row.id,
                source_id=row.source_id,
                status=row.status,
                started_at=row.started_at,
                finished_at=row.finished_at,
                stats=row.stats,
                revision_id=row.revision_id,
                proposal_id=row.proposal_id,
                error=row.error,
            )
        )

    async def get(self, run_id: str) -> SourceRunRow | None:
        found = (
            (
                await self._session.execute(
                    select(SOURCE_RUNS).where(
                        SOURCE_RUNS.c.domain_id == self._domain, SOURCE_RUNS.c.id == run_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if found is None else _row(found)

    async def finish(
        self,
        run_id: str,
        *,
        status: RunStatus,
        finished_at: datetime,
        stats: dict[str, Any],
        revision_id: bytes | None = None,
        proposal_id: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        await self._session.execute(
            update(SOURCE_RUNS)
            .where(SOURCE_RUNS.c.domain_id == self._domain, SOURCE_RUNS.c.id == run_id)
            .values(
                status=status,
                finished_at=finished_at,
                stats=stats,
                revision_id=revision_id,
                proposal_id=proposal_id,
                error=error,
            )
        )

    async def latest(
        self, source_id: str, *, status: RunStatus | None = None
    ) -> SourceRunRow | None:
        """The most recently started run of a Source (with `status`, when given)."""
        query = select(SOURCE_RUNS).where(
            SOURCE_RUNS.c.domain_id == self._domain, SOURCE_RUNS.c.source_id == source_id
        )
        if status is not None:
            query = query.where(SOURCE_RUNS.c.status == status)
        query = query.order_by(SOURCE_RUNS.c.started_at.desc(), SOURCE_RUNS.c.id.desc()).limit(1)
        found = (await self._session.execute(query)).mappings().one_or_none()
        return None if found is None else _row(found)

    async def page(
        self, source_id: str, *, limit: int, after: tuple[datetime, str] | None = None
    ) -> list[SourceRunRow]:
        """Runs of a Source, newest first, after the keyset `(started_at, id)`."""
        table = SOURCE_RUNS
        query = select(table).where(
            table.c.domain_id == self._domain, table.c.source_id == source_id
        )
        if after is not None:
            at, last_id = after
            query = query.where(
                or_(table.c.started_at < at, and_(table.c.started_at == at, table.c.id < last_id))
            )
        query = query.order_by(table.c.started_at.desc(), table.c.id.desc()).limit(limit)
        return [_row(row) for row in (await self._session.execute(query)).mappings()]
