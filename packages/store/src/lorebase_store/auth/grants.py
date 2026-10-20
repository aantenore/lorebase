"""Role grants of principals and groups in a Domain (design 10.2).

Grants are Domain rows under row-level security: each lookup names its Domain, or the system
scope when it spans Domains.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.auth.scope import new_id, rls_scope
from lorebase_store.models import Domain, Grant
from lorebase_store.models.enums import GrantScope

ID_PREFIX = "grt"


@dataclass(frozen=True)
class GrantRecord:
    """A role of `subject` (principal or group id) in a Domain, optionally narrowed by scope."""

    id: str
    domain_id: str
    subject: str
    role: str
    scope_type: GrantScope
    scope_ref: str | None
    created_by: str
    created_at: datetime


def _record(row: Grant) -> GrantRecord:
    return GrantRecord(
        id=row.id,
        domain_id=row.domain_id,
        subject=row.subject,
        role=row.role,
        scope_type=row.scope_type,
        scope_ref=row.scope_ref,
        created_by=row.created_by,
        created_at=row.created_at,
    )


class GrantRepository:
    """Grants by Domain and subject."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        domain_id: str,
        subject: str,
        role: str,
        created_by: str,
        scope_type: GrantScope = GrantScope.DOMAIN,
        scope_ref: str | None = None,
    ) -> GrantRecord:
        """Insert a grant, or return the identical one that already exists.

        `scope_ref` is required for every scope but `domain`, where it must be absent.
        """
        if (scope_type is GrantScope.DOMAIN) != (scope_ref is None):
            raise ValueError("scope_ref is required for kind, operation and branch scopes only")
        async with rls_scope(self._session, domain_ids=[domain_id]):
            existing = (
                await self._session.execute(
                    select(Grant).where(
                        Grant.domain_id == domain_id,
                        Grant.subject == subject,
                        Grant.role == role,
                        Grant.scope_type == scope_type,
                        Grant.scope_ref.is_not_distinct_from(scope_ref),
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return _record(existing)
            row = Grant(
                id=new_id(ID_PREFIX),
                domain_id=domain_id,
                subject=subject,
                role=role,
                scope_type=scope_type,
                scope_ref=scope_ref,
                created_by=created_by,
            )
            self._session.add(row)
            await self._session.flush()
            await self._session.refresh(row)
            return _record(row)

    async def get(self, domain_id: str, grant_id: str) -> GrantRecord | None:
        """A grant of the Domain."""
        async with rls_scope(self._session, domain_ids=[domain_id]):
            row = (
                await self._session.execute(
                    select(Grant).where(Grant.domain_id == domain_id, Grant.id == grant_id)
                )
            ).scalar_one_or_none()
        return _record(row) if row else None

    async def delete(self, domain_id: str, grant_id: str) -> bool:
        """Revoke a grant; false when the Domain has no such grant."""
        async with rls_scope(self._session, domain_ids=[domain_id]):
            result = await self._session.execute(
                delete(Grant).where(Grant.domain_id == domain_id, Grant.id == grant_id)
            )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def for_subjects(self, domain_id: str, subjects: Collection[str]) -> list[GrantRecord]:
        """Every grant in the Domain held by any of `subjects`."""
        if not subjects:
            return []
        async with rls_scope(self._session, domain_ids=[domain_id]):
            rows = await self._session.execute(
                select(Grant)
                .where(Grant.domain_id == domain_id, Grant.subject.in_(list(subjects)))
                .order_by(Grant.created_at, Grant.id)
            )
            return [_record(row) for row in rows.scalars()]

    async def domain_exists(self, domain_id: str) -> bool:
        """Whether a Domain with this id exists (Domains are not under row-level security)."""
        found = await self._session.execute(select(Domain.id).where(Domain.id == domain_id))
        return found.scalar_one_or_none() is not None

    async def for_subjects_anywhere(self, subjects: Collection[str]) -> list[GrantRecord]:
        """Every grant held by any of `subjects`, across Domains (system scope)."""
        if not subjects:
            return []
        async with rls_scope(self._session, bypass=True):
            rows = await self._session.execute(
                select(Grant)
                .where(Grant.subject.in_(list(subjects)))
                .order_by(Grant.domain_id, Grant.created_at, Grant.id)
            )
            return [_record(row) for row in rows.scalars()]

    async def list_domain(
        self, domain_id: str, *, limit: int, after: tuple[datetime, str] | None = None
    ) -> list[GrantRecord]:
        """Grants of a Domain in creation order; `after` is `(created_at, id)` of the last item."""
        statement = (
            select(Grant)
            .where(Grant.domain_id == domain_id)
            .order_by(Grant.created_at, Grant.id)
            .limit(limit)
        )
        if after is not None:
            statement = statement.where(tuple_(Grant.created_at, Grant.id) > after)
        async with rls_scope(self._session, domain_ids=[domain_id]):
            rows = await self._session.execute(statement)
            return [_record(row) for row in rows.scalars()]
