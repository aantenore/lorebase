"""One transaction over the auth repositories."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_store.auth.api_keys import ApiKeyRepository
from lorebase_store.auth.audit import AuditRepository
from lorebase_store.auth.grants import GrantRepository
from lorebase_store.auth.principals import GroupRepository, PrincipalRepository


@dataclass(frozen=True)
class AuthUnit:
    """The auth repositories sharing one session and transaction."""

    principals: PrincipalRepository
    groups: GroupRepository
    api_keys: ApiKeyRepository
    grants: GrantRepository
    audit: AuditRepository


class SqlAuthStore:
    """Opens transactions over the auth repositories. Commits on success, rolls back on error."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @asynccontextmanager
    async def unit(self) -> AsyncIterator[AuthUnit]:
        """A transaction: leave the block normally to commit."""
        async with self._sessions() as session, session.begin():
            yield AuthUnit(
                principals=PrincipalRepository(session),
                groups=GroupRepository(session),
                api_keys=ApiKeyRepository(session),
                grants=GrantRepository(session),
                audit=AuditRepository(session),
            )
