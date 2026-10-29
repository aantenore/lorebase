"""Domains (design 5.2, 7.1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Table, delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.models import metadata

DOMAINS: Table = metadata.tables["domains"]


@dataclass(frozen=True, slots=True)
class DomainRow:
    id: str
    display_name: str
    description: str | None
    default_branch: str
    leaf_size: int
    settings: dict[str, Any]
    created_at: datetime
    created_by: str


class DomainNotFoundError(LookupError):
    """The Domain does not exist."""


class DomainRepository:
    """Installation-wide Domain rows (no row-level security on this table)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, row: DomainRow) -> DomainRow:
        await self._session.execute(
            insert(DOMAINS).values(
                id=row.id,
                display_name=row.display_name,
                description=row.description,
                default_branch=row.default_branch,
                leaf_size=row.leaf_size,
                settings=row.settings,
                created_at=row.created_at,
                created_by=row.created_by,
            )
        )
        return row

    async def get(self, domain_id: str) -> DomainRow | None:
        result = await self._session.execute(select(DOMAINS).where(DOMAINS.c.id == domain_id))
        found = result.mappings().one_or_none()
        return None if found is None else DomainRow(**found)

    async def require(self, domain_id: str) -> DomainRow:
        found = await self.get(domain_id)
        if found is None:
            raise DomainNotFoundError(domain_id)
        return found

    async def list(self, *, limit: int, after: str | None = None) -> list[DomainRow]:
        """Domains ordered by id, starting after `after`."""
        query = select(DOMAINS).order_by(DOMAINS.c.id).limit(limit)
        if after is not None:
            query = query.where(DOMAINS.c.id > after)
        result = await self._session.execute(query)
        return [DomainRow(**row) for row in result.mappings()]

    async def update(
        self,
        domain_id: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> DomainRow | None:
        """Change display metadata or replace the settings; `None` leaves a field as it is.
        Returns the updated row."""
        values: dict[str, Any] = {}
        if settings is not None:
            values["settings"] = settings
        if display_name is not None:
            values["display_name"] = display_name
        if description is not None:
            values["description"] = description or None
        if values:
            await self._session.execute(
                update(DOMAINS).where(DOMAINS.c.id == domain_id).values(**values)
            )
        return await self.get(domain_id)

    async def delete(self, domain_id: str) -> bool:
        """Delete a Domain; every Domain-scoped row goes with it (foreign key cascade)."""
        result = await self._session.execute(
            delete(DOMAINS).where(DOMAINS.c.id == domain_id).returning(DOMAINS.c.id)
        )
        return result.scalar_one_or_none() is not None
