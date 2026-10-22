"""Browser sessions of the web app (design 13.2, Keeper as backend-for-frontend).

The repository stores opaque values: the session id digest and the encrypted
provider tokens are produced by Keeper. A session is found by the SHA-256 of its id.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.models import WebSession


@dataclass(frozen=True)
class SessionRecord:
    """A stored session: a digest and ciphertext only, never the session id."""

    id_hash: bytes
    principal_id: str
    subject: str
    external_groups: tuple[str, ...]
    tokens: bytes | None
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    refresh_at: datetime | None


def _record(row: WebSession) -> SessionRecord:
    return SessionRecord(
        id_hash=row.id_hash,
        principal_id=row.principal_id,
        subject=row.subject,
        external_groups=tuple(str(group) for group in row.external_groups or ()),
        tokens=row.tokens,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        expires_at=row.expires_at,
        refresh_at=row.refresh_at,
    )


class SessionRepository:
    """Web sessions. The table is installation-wide: no Domain scope applies."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        id_hash: bytes,
        principal_id: str,
        subject: str,
        external_groups: Sequence[str],
        tokens: bytes | None,
        created_at: datetime,
        expires_at: datetime,
        refresh_at: datetime | None,
    ) -> SessionRecord:
        """Insert a session first seen at `created_at`."""
        row = WebSession(
            id_hash=id_hash,
            principal_id=principal_id,
            subject=subject,
            external_groups=list(external_groups),
            tokens=tokens,
            created_at=created_at,
            last_seen_at=created_at,
            expires_at=expires_at,
            refresh_at=refresh_at,
        )
        self._session.add(row)
        await self._session.flush()
        return _record(row)

    async def get(self, id_hash: bytes) -> SessionRecord | None:
        """The session whose id has this digest."""
        row = await self._session.get(WebSession, id_hash)
        return _record(row) if row else None

    async def touch(self, id_hash: bytes, at: datetime, *, min_interval: timedelta) -> bool:
        """Record activity at `at`, unless the recorded activity is more recent than
        `min_interval`; keeps busy sessions from writing on every request."""
        result = await self._session.execute(
            update(WebSession)
            .where(WebSession.id_hash == id_hash, WebSession.last_seen_at <= at - min_interval)
            .values(last_seen_at=at)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def claim_refresh(self, id_hash: bytes, now: datetime, until: datetime) -> bool:
        """Take the right to refresh the provider tokens: true for one caller only while the
        refresh is due, which moves the due time to `until` (a lease)."""
        result = await self._session.execute(
            update(WebSession)
            .where(
                WebSession.id_hash == id_hash,
                WebSession.refresh_at.is_not(None),
                WebSession.refresh_at <= now,
            )
            .values(refresh_at=until)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def update_tokens(
        self,
        id_hash: bytes,
        *,
        tokens: bytes | None,
        external_groups: Sequence[str],
        refresh_at: datetime | None,
    ) -> bool:
        """Store refreshed tokens and groups; false when the session is gone."""
        result = await self._session.execute(
            update(WebSession)
            .where(WebSession.id_hash == id_hash)
            .values(tokens=tokens, external_groups=list(external_groups), refresh_at=refresh_at)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def delete(self, id_hash: bytes) -> bool:
        """End a session; false when it did not exist."""
        result = await self._session.execute(
            delete(WebSession).where(WebSession.id_hash == id_hash)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def delete_for_principal(self, principal_id: str) -> int:
        """End every session of a principal; the number ended."""
        result = await self._session.execute(
            delete(WebSession).where(WebSession.principal_id == principal_id)
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    async def purge_expired(self, now: datetime, idle_before: datetime, *, limit: int) -> int:
        """Delete up to `limit` sessions past their absolute expiry or idle since before
        `idle_before`; the number deleted."""
        stale = (
            select(WebSession.id_hash)
            .where(or_(WebSession.expires_at <= now, WebSession.last_seen_at <= idle_before))
            .limit(limit)
            .scalar_subquery()
        )
        result = await self._session.execute(
            delete(WebSession).where(WebSession.id_hash.in_(stale))
        )
        return int(result.rowcount)  # type: ignore[attr-defined]
