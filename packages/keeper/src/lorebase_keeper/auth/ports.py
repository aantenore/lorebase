"""What Keeper's auth code needs from persistence.

`lorebase_store.auth.SqlAuthStore` implements these protocols; tests substitute an in-memory
store. Only the methods Keeper calls are listed.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import date, datetime, timedelta
from typing import Protocol

from lorebase_store.auth import (
    ApiKeyRecord,
    AuditRecord,
    GrantRecord,
    NewAuditEntry,
    PrincipalRecord,
    UsageRecord,
)
from lorebase_store.models.enums import GrantScope, PrincipalType


class PrincipalsPort(Protocol):
    """Principals."""

    async def get(self, principal_id: str) -> PrincipalRecord | None: ...

    async def get_by_subject(self, external_subject: str) -> PrincipalRecord | None: ...

    async def add(
        self,
        type_: PrincipalType,
        display_name: str,
        *,
        external_subject: str | None = None,
        principal_id: str | None = None,
    ) -> PrincipalRecord: ...

    async def get_or_create_user(
        self, external_subject: str, display_name: str
    ) -> tuple[PrincipalRecord, bool]: ...

    async def list(
        self,
        *,
        type_: PrincipalType | None = None,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> list[PrincipalRecord]: ...


class GroupsPort(Protocol):
    """Groups."""

    async def group_ids_for(
        self, principal_id: str, external_groups: Collection[str] = ()
    ) -> list[str]: ...

    async def subject_exists(self, subject_id: str) -> bool: ...


class ApiKeysPort(Protocol):
    """API keys."""

    async def add(
        self,
        *,
        principal_id: str,
        prefix: str,
        key_hash: bytes,
        scopes: Sequence[str] = (),
        expires_at: datetime | None = None,
    ) -> ApiKeyRecord: ...

    async def get(self, key_id: str) -> ApiKeyRecord | None: ...

    async def get_by_prefix(self, prefix: str) -> ApiKeyRecord | None: ...

    async def touch(self, key_id: str, at: datetime, *, min_interval: timedelta) -> bool: ...

    async def revoke(self, key_id: str, at: datetime) -> bool: ...

    async def list_for_scope(
        self, scope: str, *, limit: int, after: tuple[datetime, str] | None = None
    ) -> list[ApiKeyRecord]: ...

    async def list_for_principals(self, principal_ids: Collection[str]) -> list[ApiKeyRecord]: ...


class GrantsPort(Protocol):
    """Grants."""

    async def add(
        self,
        *,
        domain_id: str,
        subject: str,
        role: str,
        created_by: str,
        scope_type: GrantScope = GrantScope.DOMAIN,
        scope_ref: str | None = None,
    ) -> GrantRecord: ...

    async def get(self, domain_id: str, grant_id: str) -> GrantRecord | None: ...

    async def delete(self, domain_id: str, grant_id: str) -> bool: ...

    async def for_subjects(
        self, domain_id: str, subjects: Collection[str]
    ) -> list[GrantRecord]: ...

    async def for_subjects_anywhere(self, subjects: Collection[str]) -> list[GrantRecord]: ...

    async def domain_exists(self, domain_id: str) -> bool: ...

    async def list_domain(
        self, domain_id: str, *, limit: int, after: tuple[datetime, str] | None = None
    ) -> list[GrantRecord]: ...


class AuditPort(Protocol):
    """Audit log."""

    async def append(self, entry: NewAuditEntry) -> AuditRecord: ...

    async def list(
        self, *, domain_id: str | None, limit: int, before_seq: int | None = None
    ) -> list[AuditRecord]: ...


class UsagePort(Protocol):
    """Daily usage counters of API keys."""

    async def get(self, api_key_id: str, day: date) -> UsageRecord: ...

    async def add(
        self, api_key_id: str, day: date, *, requests: int, context_tokens: int, at: datetime
    ) -> UsageRecord: ...


class AuthUnitPort(Protocol):
    """Repositories sharing one transaction."""

    @property
    def principals(self) -> PrincipalsPort: ...

    @property
    def groups(self) -> GroupsPort: ...

    @property
    def api_keys(self) -> ApiKeysPort: ...

    @property
    def grants(self) -> GrantsPort: ...

    @property
    def audit(self) -> AuditPort: ...

    @property
    def usage(self) -> UsagePort: ...


class AuthStore(Protocol):
    """Opens transactions; leaving the block normally commits, an exception rolls back."""

    def unit(self) -> AbstractAsyncContextManager[AuthUnitPort]: ...
