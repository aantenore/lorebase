"""Authorization of reads against the Revision actually read (design 10.2).

Every read path (records and links, query and search, Revisions, log and diff, schema, branch
listings) resolves what it reads first and then checks the permission against it:

- a grant not narrowed to a branch (Domain-wide, or narrowed to the Kind being read) covers
  every Revision; a grant narrowed to a Kind covers only that Kind, so the record reads
  check every Kind a request touches against `ReadGuard.granted_kinds`;
- a branch, or `at` / a diff side naming a branch, is checked with
  `AccessContext(branch=<that branch>)`;
- a tag or a bare Revision id is a pointer into the history: it is allowed when the caller may
  read some branch from whose head the Revision is reachable, following parents for at most
  `keeper.reads.reachability_max_depth` hops.

Callers with narrowed grants get 403 for everything they may not read, including unknown names
and Revisions, so the answer does not tell whether a Revision exists.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_keeper.auth.deps import missing_permission, refusal
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.rbac import AccessContext, Authorizer
from lorebase_keeper.context import StoreAccess, require_domain, resolve_revision
from lorebase_keeper.errors import FORBIDDEN, ProblemError
from lorebase_store.models.enums import RefKind
from lorebase_store.repositories import DomainRow, RefRepository, RevisionRepository


@dataclass(frozen=True)
class ReadTarget:
    """What a read resolved to: a Revision, and the ref it was read through (none for a bare
    Revision id)."""

    revision: bytes
    ref: str | None = None
    kind: RefKind | None = None


class ReadSource(Protocol):
    """The store as seen by read authorization, inside one transaction."""

    async def lookup(self, *, branch: str | None, at: str | None) -> ReadTarget:
        """Resolve a branch (default: the Domain's) or `at` (Revision id, tag or branch name);
        a 404 `ProblemError` when unknown."""
        ...

    async def branch_heads(self) -> Mapping[str, bytes]:
        """Head Revision of every branch of the Domain."""
        ...

    async def parents(self, revision: bytes) -> Sequence[bytes]: ...

    async def generations(self, revisions: Iterable[bytes]) -> Mapping[bytes, int]: ...


class StoreReadSource:
    """`ReadSource` over the repositories of one Domain."""

    def __init__(self, session: AsyncSession, domain: DomainRow) -> None:
        self._session = session
        self._domain = domain
        self._revisions = RevisionRepository(session, domain.id)

    async def lookup(self, *, branch: str | None, at: str | None) -> ReadTarget:
        resolved = await resolve_revision(self._session, self._domain, branch=branch, at=at)
        ref = resolved.ref
        return ReadTarget(
            revision=resolved.revision.id,
            ref=None if ref is None else ref.name,
            kind=None if ref is None else ref.kind,
        )

    async def branch_heads(self) -> Mapping[str, bytes]:
        refs = await RefRepository(self._session, self._domain.id).all(RefKind.BRANCH)
        return {ref.name: ref.head for ref in refs}

    async def parents(self, revision: bytes) -> Sequence[bytes]:
        return await self._revisions.parents(revision)

    async def generations(self, revisions: Iterable[bytes]) -> Mapping[bytes, int]:
        return await self._revisions.generations(revisions)


async def reachable(
    source: ReadSource, heads: Collection[bytes], target: bytes, max_depth: int
) -> bool:
    """Whether `target` is one of `heads` or an ancestor of one within `max_depth` parent hops."""
    floor = (await source.generations([target])).get(target)
    if floor is None:
        return False
    frontier = set(heads)
    seen = set(frontier)
    for depth in range(max_depth + 1):
        if target in frontier:
            return True
        if depth == max_depth or not frontier:
            return False
        generations = await source.generations(frontier)
        # Only a Revision above the target in the history can have it as an ancestor.
        above = [r for r in frontier if generations.get(r, -1) > floor]
        parents = {p for r in above for p in await source.parents(r)}
        frontier = parents - seen
        seen |= frontier
    return False


@dataclass(frozen=True)
class ReadGuard:
    """A caller reading one Domain, whose permissions are checked against what is read.

    `kind` is the Kind the request reads, when it names one, so grants narrowed to that Kind
    apply.
    """

    authorizer: Authorizer
    principal: Principal
    permissions: tuple[Permission, ...]
    domain_id: str
    max_depth: int
    kind: str | None = None

    async def may(self, branch: str | None = None) -> bool:
        """Every permission for `branch`; `None` asks for grants not narrowed to branches."""
        context = AccessContext(kind=self.kind, branch=branch)
        return all(
            [
                await self.authorizer.allows(self.principal, p, self.domain_id, context)
                for p in self.permissions
            ]
        )

    async def wide(self) -> bool:
        """Whether the grants cover every Revision (none of them is narrowed to branches)."""
        return await self.may(None)

    async def require_wide(self) -> None:
        """403 unless the grants cover the whole Domain: for Domain-wide reads (Source runs)
        that grants narrowed to a Kind or a branch must not see."""
        if not await self.wide():
            raise self.denied()

    async def granted_kinds(self) -> frozenset[str]:
        """Kinds the caller may read through grants narrowed to a Kind: every Revision, those
        Kinds only."""
        found: frozenset[str] | None = None
        for permission in self.permissions:
            kinds = await self.authorizer.granted_kinds(self.principal, permission, self.domain_id)
            found = kinds if found is None else found & kinds
        return found or frozenset()

    def kind_denied(self, kind: str) -> ProblemError:
        """403 for a Kind outside the Kinds a Kind-narrowed caller may read."""
        permission = self.permissions[0]
        return ProblemError(
            FORBIDDEN,
            f"Missing permission {permission.value} on Kind {kind} in Domain {self.domain_id}.",
            extensions={"permission": permission.value, "domain": self.domain_id, "kind": kind},
        )

    async def precheck(self) -> None:
        """403 before any lookup when no grant of the caller holds a permission at all; 404
        instead when the Domain does not exist and the caller may be told so."""
        for permission in self.permissions:
            if not await self.authorizer.allows_anywhere(
                self.principal, permission, self.domain_id
            ):
                raise await refusal(self.authorizer, self.principal, permission, self.domain_id)

    def denied(self) -> ProblemError:
        return missing_permission(self.permissions[0], self.domain_id)

    async def require_branch(self, name: str) -> None:
        if not await self.may(name):
            raise self.denied()

    async def require(self, source: ReadSource, target: ReadTarget, *, wide: bool) -> None:
        """Require the permissions for the Revision `target` resolved to (see module doc)."""
        if wide:
            return
        if target.kind is RefKind.BRANCH:
            assert target.ref is not None
            await self.require_branch(target.ref)
            return
        readable = [
            head for name, head in (await source.branch_heads()).items() if await self.may(name)
        ]
        if readable and await reachable(source, readable, target.revision, self.max_depth):
            return
        raise self.denied()

    def unknown(self, error: ProblemError, *, wide: bool) -> ProblemError:
        """The answer to a lookup that found nothing: the 404 itself only for wide callers."""
        if wide or error.status_code != 404:
            return error
        return self.denied()


async def authorize_read(
    source: ReadSource,
    authorizer: Authorizer,
    principal: Principal,
    permissions: Sequence[Permission],
    domain_id: str,
    *,
    branch: str | None,
    at: str | None,
    wide: bool,
    max_depth: int,
    kind: str | None = None,
) -> ReadTarget:
    """Resolve `branch`/`at` and require every permission for the Revision it resolved to.

    `wide` tells whether the caller holds the permissions through grants not narrowed to
    branches, which cover every Revision; the lookup then answers 404 for unknown names.
    Otherwise unknown names are refused as forbidden, like Revisions the caller may not read.
    """
    guard = ReadGuard(authorizer, principal, tuple(permissions), domain_id, max_depth, kind)
    try:
        target = await source.lookup(branch=branch, at=at)
    except ProblemError as error:
        raise guard.unknown(error, wide=wide) from None
    await guard.require(source, target, wide=wide)
    return target


async def resolve_readable(
    store: StoreAccess,
    authorizer: Authorizer,
    principal: Principal,
    permissions: Sequence[Permission],
    domain_id: str,
    *,
    branch: str | None,
    at: str | None,
    max_depth: int,
    kind: str | None = None,
) -> ReadTarget:
    """The Revision a request reads, after requiring `permissions` for it.

    403 (naming the first permission) when the caller lacks them for what `branch`/`at`
    resolve to; 404 for unknown names only to callers whose grants are Domain-wide.
    """
    guard = ReadGuard(authorizer, principal, tuple(permissions), domain_id, max_depth, kind)
    await guard.precheck()
    wide = await guard.wide()
    try:
        async with store.sessions() as session, session.begin():
            domain = await require_domain(session, domain_id)
            return await authorize_read(
                StoreReadSource(session, domain),
                authorizer,
                principal,
                permissions,
                domain_id,
                branch=branch,
                at=at,
                wide=wide,
                max_depth=max_depth,
                kind=kind,
            )
    except ProblemError as error:
        raise guard.unknown(error, wide=wide) from None


async def resolve_kind_limited(
    store: StoreAccess, guard: ReadGuard, *, branch: str | None, at: str | None
) -> ReadTarget:
    """The Revision a caller holding grants narrowed to Kinds reads (they cover every branch
    and Revision, for those Kinds). Unknown Domains, branches and Revisions answer 403, like
    names the caller may not read."""
    try:
        async with store.sessions() as session, session.begin():
            domain = await require_domain(session, guard.domain_id)
            return await StoreReadSource(session, domain).lookup(branch=branch, at=at)
    except ProblemError as error:
        if error.status_code == 404:
            raise guard.denied() from None
        raise
