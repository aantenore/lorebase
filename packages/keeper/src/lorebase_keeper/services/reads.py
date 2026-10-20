"""Authorization of reads against the Revision actually read (design 10.2).

A request that names a branch, a tag or a Revision id (`branch`, `at`, a branch `source`) is
resolved first; the permission is then checked against what the name resolved to:

- a Domain-wide grant (not narrowed to a branch, Kind or Operation) covers every Revision;
- a branch, or `at` naming a branch, is checked with `AccessContext(branch=<that branch>)`;
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

from lorebase_keeper.auth.deps import missing_permission
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.rbac import NO_CONTEXT, AccessContext, Authorizer
from lorebase_keeper.context import StoreAccess, require_domain, resolve_revision
from lorebase_keeper.errors import ProblemError
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
) -> ReadTarget:
    """Resolve `branch`/`at` and require every permission for the Revision it resolved to.

    `wide` tells whether the caller holds the permissions through Domain-wide grants, which
    cover every Revision; the lookup then answers 404 for unknown names. Otherwise unknown
    names are refused as forbidden, like Revisions the caller may not read.
    """
    try:
        target = await source.lookup(branch=branch, at=at)
    except ProblemError as error:
        if wide or error.status_code != 404:
            raise
        raise missing_permission(permissions[0], domain_id) from None
    if wide:
        return target

    async def may(context: AccessContext) -> bool:
        return all([await authorizer.allows(principal, p, domain_id, context) for p in permissions])

    if target.kind is RefKind.BRANCH:
        assert target.ref is not None
        if await may(AccessContext(branch=target.ref)):
            return target
        raise missing_permission(permissions[0], domain_id)

    readable = [
        head
        for name, head in (await source.branch_heads()).items()
        if await may(AccessContext(branch=name))
    ]
    if readable and await reachable(source, readable, target.revision, max_depth):
        return target
    raise missing_permission(permissions[0], domain_id)


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
) -> ReadTarget:
    """The Revision a request reads, after requiring `permissions` for it.

    403 (naming the first permission) when the caller lacks them for what `branch`/`at`
    resolve to; 404 for unknown names only to callers whose grants are Domain-wide.
    """
    for permission in permissions:
        if not await authorizer.allows_anywhere(principal, permission, domain_id):
            raise missing_permission(permission, domain_id)
    wide = all([await authorizer.allows(principal, p, domain_id, NO_CONTEXT) for p in permissions])
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
            )
    except ProblemError as error:
        if not wide and error.status_code == 404:
            raise missing_permission(permissions[0], domain_id) from None
        raise
