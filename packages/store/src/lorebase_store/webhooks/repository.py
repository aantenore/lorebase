"""Webhooks of one Domain (management); deliveries are handled by `webhooks.delivery`."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Table, delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.auth.scope import new_id
from lorebase_store.models import metadata
from lorebase_store.webhooks.keys import StoredKey

WEBHOOKS: Table = metadata.tables["webhooks"]
WEBHOOK_DELIVERIES: Table = metadata.tables["webhook_deliveries"]
ID_PREFIX = "whk"


@dataclass(frozen=True, slots=True)
class WebhookRow:
    id: str
    domain_id: str
    url: str
    description: str | None
    event_types: tuple[str, ...]
    branches: tuple[str, ...] | None
    created_by: str
    created_at: datetime


_COLUMNS = (
    WEBHOOKS.c.id,
    WEBHOOKS.c.domain_id,
    WEBHOOKS.c.url,
    WEBHOOKS.c.description,
    WEBHOOKS.c.event_types,
    WEBHOOKS.c.branches,
    WEBHOOKS.c.created_by,
    WEBHOOKS.c.created_at,
)


def webhook_row(row: object) -> WebhookRow:
    values = row._mapping  # type: ignore[attr-defined]
    branches = values["branches"]
    return WebhookRow(
        id=values["id"],
        domain_id=values["domain_id"],
        url=values["url"],
        description=values["description"],
        event_types=tuple(values["event_types"]),
        branches=None if branches is None else tuple(branches),
        created_by=values["created_by"],
        created_at=values["created_at"],
    )


class WebhookRepository:
    """Webhooks of one Domain, in the caller's transaction (scoped to the Domain)."""

    def __init__(self, session: AsyncSession, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id

    async def add(
        self,
        *,
        url: str,
        description: str | None,
        event_types: Sequence[str],
        branches: Sequence[str] | None,
        signing_key: StoredKey,
        created_by: str,
    ) -> WebhookRow:
        result = await self._session.execute(
            insert(WEBHOOKS)
            .values(
                domain_id=self._domain,
                id=new_id(ID_PREFIX),
                url=url,
                description=description,
                event_types=list(event_types),
                branches=None if branches is None else list(branches),
                secret_hash=signing_key.secret_hash,
                sealed_key=signing_key.sealed_key,
                created_by=created_by,
            )
            .returning(*_COLUMNS)
        )
        return webhook_row(result.one())

    async def all(self) -> list[WebhookRow]:
        result = await self._session.execute(
            select(*_COLUMNS)
            .where(WEBHOOKS.c.domain_id == self._domain)
            .order_by(WEBHOOKS.c.created_at, WEBHOOKS.c.id)
        )
        return [webhook_row(row) for row in result]

    async def count(self) -> int:
        found = await self._session.scalar(
            select(func.count()).select_from(WEBHOOKS).where(WEBHOOKS.c.domain_id == self._domain)
        )
        return int(found or 0)

    async def delete(self, webhook_id: str) -> bool:
        """Delete a webhook and its deliveries; false when it does not exist."""
        result = await self._session.execute(
            delete(WEBHOOKS)
            .where(WEBHOOKS.c.domain_id == self._domain, WEBHOOKS.c.id == webhook_id)
            .returning(WEBHOOKS.c.id)
        )
        return result.first() is not None
