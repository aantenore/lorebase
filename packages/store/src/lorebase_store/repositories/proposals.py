"""Proposals and their reviews (design 7.3, 10.3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Table, and_, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.models import metadata
from lorebase_store.models.enums import ProposalOrigin, ProposalStatus, ReviewVerdict

PROPOSALS: Table = metadata.tables["proposals"]
REVIEWS: Table = metadata.tables["proposal_reviews"]


@dataclass(frozen=True, slots=True)
class ProposalRow:
    id: str
    title: str
    description: str | None
    source_ref: str
    target_ref: str
    origin: ProposalOrigin
    status: ProposalStatus
    author: str
    created_at: datetime
    merged_revision: bytes | None = None
    checks: dict[str, Any] | None = None
    policies: dict[str, Any] | None = None
    idempotency_key: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProposalFilter:
    """Optional filters of a Proposal listing."""

    status: ProposalStatus | None = None
    origin: ProposalOrigin | None = None
    source_ref: str | None = None
    target_ref: str | None = None
    author: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewRow:
    id: str
    proposal_id: str
    reviewer: str
    verdict: ReviewVerdict
    comment: str | None
    at: datetime


def _proposal(found: Any) -> ProposalRow:
    return ProposalRow(**{k: v for k, v in found.items() if k != "domain_id"})


class ProposalRepository:
    """Proposals of one Domain."""

    def __init__(self, session: AsyncSession, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id

    async def insert(self, row: ProposalRow) -> None:
        await self._session.execute(
            insert(PROPOSALS).values(
                domain_id=self._domain,
                id=row.id,
                title=row.title,
                description=row.description,
                source_ref=row.source_ref,
                target_ref=row.target_ref,
                origin=row.origin,
                status=row.status,
                author=row.author,
                created_at=row.created_at,
                merged_revision=row.merged_revision,
                checks=row.checks,
                policies=row.policies,
                idempotency_key=row.idempotency_key,
                updated_at=row.updated_at,
            )
        )

    async def get(self, proposal_id: str, *, for_update: bool = False) -> ProposalRow | None:
        query = select(PROPOSALS).where(
            PROPOSALS.c.domain_id == self._domain, PROPOSALS.c.id == proposal_id
        )
        if for_update:
            query = query.with_for_update()
        found = (await self._session.execute(query)).mappings().one_or_none()
        return None if found is None else _proposal(found)

    async def by_key(self, author: str, idempotency_key: str) -> ProposalRow | None:
        """The Proposal an author created under an idempotency key."""
        result = await self._session.execute(
            select(PROPOSALS).where(
                PROPOSALS.c.domain_id == self._domain,
                PROPOSALS.c.author == author,
                PROPOSALS.c.idempotency_key == idempotency_key,
            )
        )
        found = result.mappings().one_or_none()
        return None if found is None else _proposal(found)

    async def page(
        self,
        filters: ProposalFilter,
        *,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> list[ProposalRow]:
        """Proposals matching `filters`, newest first, after the keyset `(created_at, id)`."""
        table = PROPOSALS
        query = select(table).where(table.c.domain_id == self._domain)
        for column, value in (
            (table.c.status, filters.status),
            (table.c.origin, filters.origin),
            (table.c.source_ref, filters.source_ref),
            (table.c.target_ref, filters.target_ref),
            (table.c.author, filters.author),
        ):
            if value is not None:
                query = query.where(column == value)
        if after is not None:
            at, last_id = after
            query = query.where(
                or_(table.c.created_at < at, and_(table.c.created_at == at, table.c.id < last_id))
            )
        query = query.order_by(table.c.created_at.desc(), table.c.id.desc()).limit(limit)
        result = await self._session.execute(query)
        return [_proposal(row) for row in result.mappings()]

    async def update(self, proposal_id: str, *, at: datetime, **values: Any) -> ProposalRow | None:
        """Set columns (`title`, `description`, `status`, `merged_revision`, `checks`,
        `policies`) and `updated_at`; returns the row, `None` when it does not exist."""
        allowed = {"title", "description", "status", "merged_revision", "checks", "policies"}
        unknown = values.keys() - allowed
        if unknown:
            raise ValueError(f"cannot update {sorted(unknown)}")
        await self._session.execute(
            update(PROPOSALS)
            .where(PROPOSALS.c.domain_id == self._domain, PROPOSALS.c.id == proposal_id)
            .values(updated_at=at, **values)
        )
        return await self.get(proposal_id)


class ReviewRepository:
    """Reviews of the Proposals of one Domain."""

    def __init__(self, session: AsyncSession, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id

    async def insert(self, row: ReviewRow) -> None:
        await self._session.execute(
            insert(REVIEWS).values(
                domain_id=self._domain,
                id=row.id,
                proposal_id=row.proposal_id,
                reviewer=row.reviewer,
                verdict=row.verdict,
                comment=row.comment,
                at=row.at,
            )
        )

    async def of(self, proposal_id: str) -> list[ReviewRow]:
        """Reviews of a Proposal, oldest first."""
        result = await self._session.execute(
            select(REVIEWS)
            .where(REVIEWS.c.domain_id == self._domain, REVIEWS.c.proposal_id == proposal_id)
            .order_by(REVIEWS.c.at, REVIEWS.c.id)
        )
        return [
            ReviewRow(**{k: v for k, v in row.items() if k != "domain_id"})
            for row in result.mappings()
        ]
