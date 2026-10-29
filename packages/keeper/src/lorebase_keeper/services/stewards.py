"""Stewards (design 10.7, AC-37): who looks after which part of a Domain.

Steward roles are RBAC role names the schema declares on the Domain (`domain.stewards`) and per
Kind (`kinds.{Kind}.stewards`, replacing the Domain's for that Kind). The steward roles of a
Proposal are the union over the Kinds its changes touch (`proposals.kinds`; schema-only
Proposals use the Domain's); those of a reconciliation candidate, its Kind's.

Assignees are computed when read, never stored: the principals holding one of the object's
steward roles through a grant covering the Domain or one of its Kinds, that may read every Kind
the object touches (a Kind invisible to them excludes them) and that hold the permission to act
on it (`proposal.review` on the target branch for Proposals, `conflict.resolve` on the branch
for candidates). A caller's own groups count through its credentials (the OIDC groups claim
included); a listing of assignees finds principals through direct grants and stored group
memberships, so members known only from a provider's token are not listed. A steward role
grants nothing: it only routes work to people who can already do it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from lorebase_core.model import Schema
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.rbac import AccessContext, Authorizer
from lorebase_keeper.auth.runtime import AuthRuntime
from lorebase_keeper.services.config import StewardSettings
from lorebase_store.auth import GrantRecord
from lorebase_store.models.enums import GrantScope

Assigned = Literal["me", "any", "none"]
"""The `assigned` filter of Proposal and reconciliation listings."""

_GRANT_PAGE = 500


def steward_roles(schema: Schema | None, kind_ids: Iterable[str] | None) -> tuple[str, ...]:
    """The steward roles of work touching `kind_ids` (none: the Domain's), sorted."""
    if schema is None:
        return ()
    domain_roles = tuple(schema.domain.stewards or ())
    kinds = list(kind_ids or ())
    if not kinds:
        return tuple(sorted(set(domain_roles)))
    roles: set[str] = set()
    for kind_id in kinds:
        kind = schema.kinds.get(kind_id)
        own = None if kind is None else kind.stewards
        roles.update(domain_roles if own is None else own)
    return tuple(sorted(roles))


@dataclass(frozen=True)
class StewardSubject:
    """An object to route: its steward roles, the Kinds it touches (ids, and the api names
    grants narrowed to a Kind name), the branch it is about and the permission to act on it."""

    roles: tuple[str, ...]
    kinds: tuple[str, ...]
    branch: str
    permission: Permission
    kind_names: tuple[str, ...] = ()

    def matches(self, assigned: Assigned | None, mine: bool) -> bool:
        """Whether the object passes the `assigned` filter (`mine`: the caller is an
        assignee)."""
        if assigned is None:
            return True
        if assigned == "me":
            return mine
        if assigned == "any":
            return bool(self.roles)
        return not self.roles


@dataclass(frozen=True)
class AssigneeList:
    """Assignees shown on an item: `None` for callers without `principal.read`."""

    ids: tuple[str, ...] | None
    truncated: bool = False


@dataclass
class Stewards:
    """Assignee computations of one request (results are memoized for the request)."""

    auth: AuthRuntime
    settings: StewardSettings
    _assignees: dict[tuple[str, StewardSubject], tuple[str, ...]] = field(default_factory=dict)
    _mine: dict[tuple[str, str, StewardSubject], bool] = field(default_factory=dict)
    _lists: dict[str, bool] = field(default_factory=dict)

    @property
    def authorizer(self) -> Authorizer:
        return self.auth.authorizer

    async def is_assignee(
        self, principal: Principal, domain_id: str, subject: StewardSubject
    ) -> bool:
        """Whether `principal` is an assignee of `subject` (module doc)."""
        if not subject.roles:
            return False
        key = (principal.id, domain_id, subject)
        cached = self._mine.get(key)
        if cached is None:
            cached = await self._qualifies(principal, domain_id, subject)
            self._mine[key] = cached
        return cached

    async def _qualifies(
        self, principal: Principal, domain_id: str, subject: StewardSubject
    ) -> bool:
        authorizer = self.authorizer
        held = set(await authorizer.roles_of(principal, domain_id))
        for kind in subject.kind_names:
            held |= await authorizer.roles_of(principal, domain_id, AccessContext(kind=kind))
        if not held & set(subject.roles):
            return False
        reads = (
            [AccessContext(kind=kind, branch=subject.branch) for kind in subject.kind_names]
            if subject.kind_names
            else [AccessContext(branch=subject.branch)]
        )
        for context in reads:
            if not await authorizer.allows(principal, Permission.DATA_READ, domain_id, context):
                return False
        return await authorizer.allows(
            principal, subject.permission, domain_id, AccessContext(branch=subject.branch)
        )

    async def assignees(self, domain_id: str, subject: StewardSubject) -> tuple[str, ...]:
        """Every assignee of `subject` found through grants (module doc), sorted by id."""
        if not subject.roles or self.auth.store is None:
            return ()
        key = (domain_id, subject)
        cached = self._assignees.get(key)
        if cached is not None:
            return cached
        grants = [g for g in await self._grants(domain_id) if self._counts(g, subject)]
        found: list[str] = []
        async with self.auth.store.unit() as unit:
            subjects = sorted({g.subject for g in grants})
            members = await unit.groups.members(subjects)
            candidates = {s for s in subjects if s not in members}
            for group in members.values():
                candidates.update(group)
            people: list[Principal] = []
            for principal_id in sorted(candidates):
                record = await unit.principals.get(principal_id)
                if record is None:
                    continue
                groups = await unit.groups.group_ids_for(principal_id)
                people.append(
                    Principal(
                        id=record.id,
                        type=record.type,
                        display_name=record.display_name,
                        method="api_key",
                        subjects=(record.id, *groups),
                    )
                )
        for person in people:
            if await self.is_assignee(person, domain_id, subject):
                found.append(person.id)
        result = tuple(found)
        self._assignees[key] = result
        return result

    def _counts(self, grant: GrantRecord, subject: StewardSubject) -> bool:
        if grant.role not in subject.roles:
            return False
        if grant.scope_type is GrantScope.DOMAIN:
            return True
        return grant.scope_type is GrantScope.KIND and grant.scope_ref in subject.kind_names

    async def _grants(self, domain_id: str) -> list[GrantRecord]:
        assert self.auth.store is not None
        found: list[GrantRecord] = []
        after = None
        async with self.auth.store.unit() as unit:
            while len(found) < self.settings.max_grants_scanned:
                page = await unit.grants.list_domain(domain_id, limit=_GRANT_PAGE, after=after)
                found.extend(page)
                if len(page) < _GRANT_PAGE:
                    break
                after = (page[-1].created_at, page[-1].id)
        return found[: self.settings.max_grants_scanned]

    async def may_list(self, principal: Principal) -> bool:
        """Whether the caller sees assignee ids (`principal.read`)."""
        cached = self._lists.get(principal.id)
        if cached is None:
            cached = await self.authorizer.allows_installation(principal, Permission.PRINCIPAL_READ)
            self._lists[principal.id] = cached
        return cached

    async def listing(
        self, principal: Principal, domain_id: str, subject: StewardSubject
    ) -> AssigneeList:
        """The assignees shown to `principal` on an item (module doc)."""
        if not await self.may_list(principal):
            return AssigneeList(None)
        found = await self.assignees(domain_id, subject)
        cap = self.settings.max_assignees_listed
        return AssigneeList(found[:cap], truncated=len(found) > cap)


def _names(schema: Schema | None, kind_ids: Sequence[str]) -> tuple[str, ...]:
    """Api names of `kind_ids` (grants narrowed to a Kind name it); the id of a Kind the
    schema no longer holds, which no such grant names."""
    kinds = {} if schema is None else schema.kinds
    return tuple(kinds[k].api_name if k in kinds else k for k in kind_ids)


def proposal_subject(
    schema: Schema | None, kinds: Sequence[str] | None, target: str
) -> StewardSubject:
    """The steward subject of a Proposal into `target` touching `kinds`."""
    touched = tuple(kinds or ())
    return StewardSubject(
        roles=steward_roles(schema, touched),
        kinds=touched,
        branch=target,
        permission=Permission.PROPOSAL_REVIEW,
        kind_names=_names(schema, touched),
    )


def candidate_subject(schema: Schema | None, kind_id: str, branch: str) -> StewardSubject:
    """The steward subject of a reconciliation candidate of `kind_id` on `branch`."""
    return StewardSubject(
        roles=steward_roles(schema, [kind_id]),
        kinds=(kind_id,),
        branch=branch,
        permission=Permission.CONFLICT_RESOLVE,
        kind_names=_names(schema, [kind_id]),
    )
