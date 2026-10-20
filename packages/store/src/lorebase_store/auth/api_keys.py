"""API keys of service principals (design 13.2).

The repository stores and looks up opaque values: the key format, the HMAC and its pepper are
decided by the caller. A key is found by its public `prefix`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.auth.scope import new_id
from lorebase_store.models import ApiKey

ID_PREFIX = "key"


class PrefixTakenError(Exception):
    """Another key already uses this public prefix; generate a new key."""


@dataclass(frozen=True)
class ApiKeyRecord:
    """A stored key. `hash` is the HMAC of the secret, never the secret."""

    id: str
    principal_id: str
    prefix: str
    hash: bytes
    scopes: tuple[str, ...]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime

    def is_usable(self, now: datetime) -> bool:
        """Neither revoked nor expired at `now`."""
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > now)


def _record(row: ApiKey) -> ApiKeyRecord:
    return ApiKeyRecord(
        id=row.id,
        principal_id=row.principal_id,
        prefix=row.prefix,
        hash=row.hash,
        scopes=tuple(row.scopes or ()),
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
    )


class ApiKeyRepository:
    """API keys. The table is installation-wide; Domain scope lives in `scopes`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        principal_id: str,
        prefix: str,
        key_hash: bytes,
        scopes: Sequence[str] = (),
        expires_at: datetime | None = None,
    ) -> ApiKeyRecord:
        """Insert a key; `PrefixTakenError` when the prefix is already in use."""
        row = ApiKey(
            id=new_id(ID_PREFIX),
            principal_id=principal_id,
            prefix=prefix,
            hash=key_hash,
            scopes=list(scopes),
            expires_at=expires_at,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError as exc:
            if "prefix" in str(exc.orig):
                raise PrefixTakenError(prefix) from None
            raise
        await self._session.refresh(row)
        return _record(row)

    async def get(self, key_id: str) -> ApiKeyRecord | None:
        """The key with this id."""
        row = await self._session.get(ApiKey, key_id)
        return _record(row) if row else None

    async def get_by_prefix(self, prefix: str) -> ApiKeyRecord | None:
        """The key with this public prefix."""
        row = (
            await self._session.execute(select(ApiKey).where(ApiKey.prefix == prefix))
        ).scalar_one_or_none()
        return _record(row) if row else None

    async def touch(self, key_id: str, at: datetime, *, min_interval: timedelta) -> bool:
        """Record a use at `at`, unless the last recorded use is more recent than `min_interval`.

        Returns whether the row was updated; the interval keeps hot keys from writing on every
        request.
        """
        result = await self._session.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id)
            .where((ApiKey.last_used_at.is_(None)) | (ApiKey.last_used_at <= at - min_interval))
            .values(last_used_at=at)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def revoke(self, key_id: str, at: datetime) -> bool:
        """Revoke a key; false when it does not exist or was already revoked."""
        result = await self._session.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id, ApiKey.revoked_at.is_(None))
            .values(revoked_at=at)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def list_for_scope(
        self, scope: str, *, limit: int, after: tuple[datetime, str] | None = None
    ) -> list[ApiKeyRecord]:
        """Keys whose `scopes` contain `scope` (for example `domain:acme`), oldest first."""
        statement = (
            select(ApiKey)
            .where(ApiKey.scopes.contains([scope]))
            .order_by(ApiKey.created_at, ApiKey.id)
            .limit(limit)
        )
        if after is not None:
            statement = statement.where(tuple_(ApiKey.created_at, ApiKey.id) > after)
        return [_record(row) for row in (await self._session.execute(statement)).scalars()]
