"""Authorization of reads against what is actually read (design 10.2, 13.3).

Grants may be narrowed to branch name patterns, so `data.read` is checked after the read is
resolved, against the branch it reads:

- reading a branch head needs `data.read` covering that branch;
- naming a branch in `at` or as a diff side reads that branch's head, so it needs `data.read`
  covering that branch, whatever `branch` says;
- naming a Revision by id or tag needs `data.read` that is not narrowed to branches, or
  `data.read` on the anchor branch (`branch`) plus the Revision being reachable from that
  branch's head, found by a walk over parents bounded by `keeper.reads.reachability_max_depth`.
  A walk that hits the bound denies.

Unknown Domains, branches and Revisions answer 403 to callers who could not read them anyway, so
404 does not reveal what exists.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import AuthDep, PrincipalDep
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.rbac import AccessContext, Authorizer
from lorebase_keeper.deps import KeeperSettingsDep
from lorebase_keeper.errors import FORBIDDEN, ProblemError
from lorebase_store.models import metadata
from lorebase_store.models.enums import RefKind
from lorebase_store.repositories import RefRow, RevisionRow

REVISIONS: Table = metadata.tables["revisions"]
REVISION_PARENTS: Table = metadata.tables["revision_parents"]

PERMISSION = Permission.DATA_READ


@dataclass(frozen=True)
class ReadAccess:
    """An authenticated reader whose `data.read` is checked once the read is resolved."""

    principal: Principal
    domain_id: str
    kind: str | None
    authorizer: Authorizer
    reachability_max_depth: int

    async def allows(self, branch: str | None) -> bool:
        """`data.read` for `branch`; `None` asks for a grant not narrowed to branches."""
        context = AccessContext(kind=self.kind, branch=branch)
        return await self.authorizer.allows(self.principal, PERMISSION, self.domain_id, context)

    async def require(self, branch: str | None) -> None:
        if not await self.allows(branch):
            raise self.denied(branch)

    def denied(self, branch: str | None) -> ProblemError:
        where = f" on branch {branch}" if branch is not None else ""
        return ProblemError(
            FORBIDDEN,
            f"Missing permission {PERMISSION.value} in Domain {self.domain_id}{where}.",
            extensions={"permission": PERMISSION.value, "domain": self.domain_id},
        )

    async def may_read_any(self, anchor: str | None) -> bool:
        """Whether the caller could read something here (decides 403 versus 404)."""
        return await self.allows(None) or (anchor is not None and await self.allows(anchor))

    async def require_revision(
        self,
        session: AsyncSession,
        revision: RevisionRow,
        *,
        named: RefRow | None,
        anchor: RefRow | None,
    ) -> None:
        """Authorize reading `revision`, named through the ref `named` (if any)."""
        if named is not None and named.kind is RefKind.BRANCH:
            await self.require(named.name)
            return
        if await self.allows(None):
            return
        if (
            anchor is not None
            and await self.allows(anchor.name)
            and await reachable(
                session, self.domain_id, anchor.head, revision, self.reachability_max_depth
            )
        ):
            return
        raise ProblemError(
            FORBIDDEN,
            f"Reading a Revision by id or tag needs {PERMISSION.value} on the whole Domain "
            f"{self.domain_id}, or on a branch (given as `branch`) the Revision belongs to.",
            extensions={"permission": PERMISSION.value, "domain": self.domain_id},
        )


async def reachable(
    session: AsyncSession, domain_id: str, head: bytes, target: RevisionRow, max_depth: int
) -> bool:
    """Whether `target` is `head` or one of its ancestors within `max_depth` parent steps.

    Walks every parent level by level (one query per level) and prunes Revisions whose
    generation is not above the target's, which cannot lead to it.
    """
    if head == target.id:
        return True
    rp, r = REVISION_PARENTS, REVISIONS
    frontier = {head}
    seen = {head}
    for _ in range(max_depth):
        rows = await session.execute(
            select(rp.c.parent_id, r.c.generation)
            .join(r, (r.c.domain_id == rp.c.domain_id) & (r.c.id == rp.c.parent_id))
            .where(rp.c.domain_id == domain_id, rp.c.revision_id.in_(list(frontier)))
        )
        following: set[bytes] = set()
        for parent, generation in rows:
            if parent == target.id:
                return True
            if parent not in seen and generation > target.generation:
                seen.add(parent)
                following.add(parent)
        if not following:
            return False
        frontier = following
    return False


def read_access(*, kind_param: str | None = None) -> Callable[..., Awaitable[ReadAccess]]:
    """Dependency: the authenticated reader of the Domain named by the path (401 otherwise).

    The `data.read` check itself runs when the read point is resolved (see module doc).
    """

    async def dependency(
        request: Request, principal: PrincipalDep, auth: AuthDep, keeper: KeeperSettingsDep
    ) -> ReadAccess:
        params = request.path_params
        domain_id = str(params["domain_id"])
        annotate(request, domain_id=domain_id, permission=PERMISSION.value)
        kind = params.get(kind_param) if kind_param else None
        return ReadAccess(
            principal=principal,
            domain_id=domain_id,
            kind=None if kind is None else str(kind),
            authorizer=auth.authorizer,
            reachability_max_depth=keeper.reads.reachability_max_depth,
        )

    return dependency
