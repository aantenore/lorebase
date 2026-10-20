"""Operation invocations (design 10.1): one row per execution, idempotent per principal and key."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Table, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.models import metadata
from lorebase_store.models.enums import InvocationStatus

INVOCATIONS: Table = metadata.tables["operation_invocations"]


@dataclass(frozen=True, slots=True)
class InvocationRow:
    id: str
    operation_id: str
    principal: str
    params: dict[str, Any]
    status: InvocationStatus
    revision_id: bytes | None
    proposal_id: str | None
    idempotency_key: str | None
    created_at: datetime
    duration_ms: int | None


class InvocationRepository:
    """Invocations of one Domain."""

    def __init__(self, session: AsyncSession, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id

    async def insert(self, row: InvocationRow) -> None:
        await self._session.execute(
            insert(INVOCATIONS).values(
                domain_id=self._domain,
                id=row.id,
                operation_id=row.operation_id,
                principal=row.principal,
                params=row.params,
                status=row.status,
                revision_id=row.revision_id,
                proposal_id=row.proposal_id,
                idempotency_key=row.idempotency_key,
                created_at=row.created_at,
                duration_ms=row.duration_ms,
            )
        )

    async def by_key(self, principal: str, idempotency_key: str) -> InvocationRow | None:
        """The invocation a principal recorded under an idempotency key."""
        result = await self._session.execute(
            select(INVOCATIONS).where(
                INVOCATIONS.c.domain_id == self._domain,
                INVOCATIONS.c.principal == principal,
                INVOCATIONS.c.idempotency_key == idempotency_key,
            )
        )
        found = result.mappings().one_or_none()
        if found is None:
            return None
        return InvocationRow(**{k: v for k, v in found.items() if k != "domain_id"})
