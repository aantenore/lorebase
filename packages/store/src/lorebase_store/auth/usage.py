"""Daily usage of API keys, the counters behind per-key quotas (design 13.6).

Each key has one row per UTC day. Writers add deltas, so several Keeper processes serving the
same key sum up correctly; the row returned by an addition holds the totals of all of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.models import ApiKeyUsage


@dataclass(frozen=True)
class UsageRecord:
    """What a key used on one day."""

    api_key_id: str
    day: date
    requests: int
    context_tokens: int


class UsageRepository:
    """Daily usage counters of API keys."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, api_key_id: str, day: date) -> UsageRecord:
        """The usage of a key on a day; zeros when nothing was recorded."""
        row = (
            await self._session.execute(
                select(ApiKeyUsage.requests, ApiKeyUsage.context_tokens).where(
                    ApiKeyUsage.api_key_id == api_key_id, ApiKeyUsage.day == day
                )
            )
        ).one_or_none()
        requests, tokens = (0, 0) if row is None else (int(row[0]), int(row[1]))
        return UsageRecord(api_key_id, day, requests, tokens)

    async def add(
        self, api_key_id: str, day: date, *, requests: int, context_tokens: int, at: datetime
    ) -> UsageRecord:
        """Add to the counters of a key and day and return the new totals.

        `IntegrityError` when the key does not exist (it was deleted meanwhile).
        """
        insertion = insert(ApiKeyUsage).values(
            api_key_id=api_key_id,
            day=day,
            requests=requests,
            context_tokens=context_tokens,
            updated_at=at,
        )
        statement = insertion.on_conflict_do_update(
            index_elements=[ApiKeyUsage.api_key_id, ApiKeyUsage.day],
            set_={
                "requests": ApiKeyUsage.requests + insertion.excluded.requests,
                "context_tokens": ApiKeyUsage.context_tokens + insertion.excluded.context_tokens,
                "updated_at": insertion.excluded.updated_at,
            },
        ).returning(ApiKeyUsage.requests, ApiKeyUsage.context_tokens)
        total = (await self._session.execute(statement)).one()
        return UsageRecord(api_key_id, day, int(total[0]), int(total[1]))
