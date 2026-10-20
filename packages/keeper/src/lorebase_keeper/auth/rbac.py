"""Authorization: Domain roles, scoped grants and the permission check (design 10.2).

A grant gives a subject (a principal or a group) a role in a Domain. By default it applies to
the whole Domain; a grant may be narrowed to a Kind, an Operation or a branch name pattern
(`agent/*`), and then counts only for requests that name that Kind, Operation or branch.
Deny by default: a permission is allowed when some grant of the caller's subjects has a role
containing it and its scope covers the request.

Grants are cached per (Domain, subjects) for `authz.grants_cache_ttl_s`; changes made through
this process call `invalidate`, other processes converge within the time to live.
"""

from __future__ import annotations

import fnmatch
import time
from collections.abc import Callable
from dataclasses import dataclass

from lorebase_keeper.auth.cache import TtlCache
from lorebase_keeper.auth.config import AuthzSettings
from lorebase_keeper.auth.permissions import INSTALLATION_PERMISSIONS, Permission, role_catalog
from lorebase_keeper.auth.ports import AuthStore
from lorebase_keeper.auth.principals import Principal
from lorebase_store.auth import GrantRecord
from lorebase_store.models.enums import GrantScope

ANY_DOMAIN = "*"
DOMAIN_SCOPE_PREFIX = "domain:"


@dataclass(frozen=True)
class AccessContext:
    """What a request is about, for grants narrowed to a Kind, Operation or branch."""

    kind: str | None = None
    operation: str | None = None
    branch: str | None = None


NO_CONTEXT = AccessContext()


def domain_scope(domain_id: str) -> str:
    """The API key scope naming a Domain."""
    return f"{DOMAIN_SCOPE_PREFIX}{domain_id}"


class Authorizer:
    """Decides whether a principal holds a permission."""

    def __init__(
        self,
        settings: AuthzSettings,
        *,
        store: AuthStore | None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._roles = role_catalog(settings.custom_roles)
        self._store = store
        self._cache: TtlCache[tuple[str, tuple[str, ...]], tuple[GrantRecord, ...]] = TtlCache(
            settings.grants_cache_ttl_s, settings.grants_cache_max_entries, monotonic
        )

    @property
    def role_names(self) -> frozenset[str]:
        """Roles that can be granted: built in and custom."""
        return frozenset(self._roles)

    def invalidate(self) -> None:
        """Drop cached grants after a grant changed through this process."""
        self._cache.clear()

    async def allows(
        self,
        principal: Principal,
        permission: Permission,
        domain_id: str,
        context: AccessContext = NO_CONTEXT,
    ) -> bool:
        """Whether `principal` holds `permission` in the Domain for this request context."""
        if principal.method == "none":
            return True
        if principal.key_scopes and domain_scope(domain_id) not in principal.key_scopes:
            return False
        grants = await self._grants(principal, domain_id)
        return any(self._grant_allows(grant, permission, context) for grant in grants)

    async def allows_installation(self, principal: Principal, permission: Permission) -> bool:
        """Whether `principal` holds an installation-level permission.

        Installation administrators do. Others do when a Domain-wide grant of theirs has a
        role containing it. API keys limited to Domains never do.
        """
        if permission not in INSTALLATION_PERMISSIONS:
            raise ValueError(f"{permission.value} is not an installation-level permission")
        if principal.method == "none" or principal.installation_admin:
            return True
        if principal.key_scopes:
            return False
        grants = await self._grants(principal, ANY_DOMAIN)
        return any(
            grant.scope_type is GrantScope.DOMAIN
            and self._grant_allows(grant, permission, AccessContext())
            for grant in grants
        )

    async def _grants(self, principal: Principal, domain_id: str) -> tuple[GrantRecord, ...]:
        key = (domain_id, principal.subjects)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        assert self._store is not None, "authorization needs a store"
        async with self._store.unit() as unit:
            found = (
                await unit.grants.for_subjects_anywhere(principal.subjects)
                if domain_id == ANY_DOMAIN
                else await unit.grants.for_subjects(domain_id, principal.subjects)
            )
        grants = tuple(found)
        self._cache.set(key, grants)
        return grants

    def _grant_allows(
        self, grant: GrantRecord, permission: Permission, context: AccessContext
    ) -> bool:
        return permission in self._roles.get(grant.role, frozenset()) and _covers(grant, context)


def _covers(grant: GrantRecord, context: AccessContext) -> bool:
    ref = grant.scope_ref
    match grant.scope_type:
        case GrantScope.DOMAIN:
            return True
        case GrantScope.KIND:
            return ref is not None and context.kind == ref
        case GrantScope.OPERATION:
            return ref is not None and context.operation == ref
        case GrantScope.BRANCH:
            return (
                ref is not None
                and context.branch is not None
                and fnmatch.fnmatchcase(context.branch, ref)
            )
    return False  # pragma: no cover - exhaustive over GrantScope
