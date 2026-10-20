"""Where a read is served from: Domain, branch, Revision and the schema at that Revision.

Every read names a branch (default: the Domain's default branch) and may name `at`, a Revision
id or a tag. Without `at`, or when `at` is the branch head, the read hits the materialized
head of the branch. Otherwise it is served from the Revision tree (design 6.12): key reads and
Kind listings only, slower, and marked `served_from: tree` in responses. Every step is
authorized against what it reads (`services.reads`).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Schema
from lorebase_core.tree import PARTITION_SCHEMA
from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import AuthDep, PrincipalDep
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.deps import KeeperSettingsDep
from lorebase_keeper.errors import NOT_FOUND, ProblemError
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.services.reads import ReadGuard, ReadTarget, StoreReadSource
from lorebase_store.commit.schema import load_compiled
from lorebase_store.materialize.rebuild import load_schema
from lorebase_store.models.enums import RefKind
from lorebase_store.nodestore import NodeRepository
from lorebase_store.repositories import (
    DomainRepository,
    DomainRow,
    MaterializedRepository,
    RefRepository,
    RefRow,
    RevisionRepository,
    RevisionRow,
    scope_to_domain,
)

REVISION_HEADER = "Lorebase-Revision"
"""Response header naming the Revision a read was served from (design 8.1)."""

_REVISION_ID = re.compile(r"^[0-9a-f]{64}$")


class ServedFrom(StrEnum):
    MATERIALIZED = "materialized"
    TREE = "tree"


@dataclass(frozen=True, slots=True)
class ReadPoint:
    """A resolved read: the Domain, the branch, the Revision served and its schema."""

    domain: DomainRow
    branch: str
    revision: RevisionRow
    schema: Schema
    served_from: ServedFrom

    @property
    def materialized(self) -> bool:
        return self.served_from is ServedFrom.MATERIALIZED

    @property
    def revision_hex(self) -> str:
        return self.revision.id.hex()


def not_found(what: str) -> ProblemError:
    return ProblemError(NOT_FOUND, f"{what} not found.")


def read_guard(*, kind_param: str | None = None) -> Callable[..., Awaitable[ReadGuard]]:
    """Dependency: the authenticated reader of the Domain named by the path (401 otherwise).

    `data.read` itself is checked once the read point is resolved (`services.reads`).
    """

    async def dependency(
        request: Request, principal: PrincipalDep, auth: AuthDep, keeper: KeeperSettingsDep
    ) -> ReadGuard:
        params = request.path_params
        domain_id = str(params["domain_id"])
        annotate(request, domain_id=domain_id, permission=Permission.DATA_READ.value)
        kind = params.get(kind_param) if kind_param else None
        return ReadGuard(
            authorizer=auth.authorizer,
            principal=principal,
            permissions=(Permission.DATA_READ,),
            domain_id=domain_id,
            max_depth=keeper.reads.reachability_max_depth,
            kind=None if kind is None else str(kind),
        )

    return dependency


async def open_domain(session: AsyncSession, guard: ReadGuard) -> DomainRow:
    """The Domain of the request, with the transaction scoped to it (403 or 404 when absent)."""
    await guard.precheck()
    await scope_to_domain(session, guard.domain_id)
    domain = await DomainRepository(session).get(guard.domain_id)
    if domain is None:
        raise guard.unknown(not_found(f"Domain {guard.domain_id}"), wide=await guard.wide())
    return domain


async def open_branch(
    session: AsyncSession, guard: ReadGuard, domain: DomainRow, branch: str | None
) -> RefRow:
    """The branch read (default: the Domain's default branch), authorized for the caller."""
    name = branch or domain.default_branch
    await guard.require_branch(name)
    ref = await RefRepository(session, domain.id).get(name)
    if ref is None or ref.kind is not RefKind.BRANCH:
        raise not_found(f"Branch {name}")
    return ref


async def find_revision(
    session: AsyncSession, domain_id: str, token: str
) -> tuple[RevisionRow, RefRow | None] | None:
    """The Revision named by an id, a tag or a branch (its head), and the ref used if any."""
    revisions = RevisionRepository(session, domain_id)
    if _REVISION_ID.match(token):
        found = await revisions.get(bytes.fromhex(token))
        if found is not None:
            return found, None
    named = await RefRepository(session, domain_id).get(token)
    if named is None:
        return None
    return await revisions.require(named.head), named


async def open_revision(
    session: AsyncSession, guard: ReadGuard, domain: DomainRow, token: str
) -> RevisionRow:
    """The Revision named by `token`, authorized against what it is (see `services.reads`)."""
    wide = await guard.wide()
    found = await find_revision(session, domain.id, token)
    if found is None:
        raise guard.unknown(not_found(f"Revision {token}"), wide=wide)
    revision, named = found
    target = ReadTarget(
        revision=revision.id,
        ref=None if named is None else named.name,
        kind=None if named is None else named.kind,
    )
    await guard.require(StoreReadSource(session, domain), target, wide=wide)
    return revision


async def resolve_point(
    session: AsyncSession,
    runtime: ReadRuntime,
    guard: ReadGuard,
    *,
    branch: str | None,
    at: str | None,
) -> ReadPoint:
    """Resolve and authorize the read point of a request (see module doc)."""
    domain = await open_domain(session, guard)
    ref = await open_branch(session, guard, domain, branch)
    revision = (
        await RevisionRepository(session, domain.id).require(ref.head)
        if at is None
        else await open_revision(session, guard, domain, at)
    )
    if revision.id == ref.head:
        materialized = await MaterializedRepository(
            session, domain.id, ref.name, writes=runtime.store_settings.writes
        ).schema()
        if materialized is not None:
            schema = runtime.schemas.get(domain.id, materialized.schema_hash)
            if schema is None:
                schema = load_compiled(materialized.compiled)
                runtime.schemas.put(domain.id, materialized.schema_hash, schema)
            return ReadPoint(domain, ref.name, revision, schema, ServedFrom.MATERIALIZED)
    schema = await schema_at(session, runtime, domain.id, revision)
    return ReadPoint(domain, ref.name, revision, schema, ServedFrom.TREE)


async def schema_at(
    session: AsyncSession, runtime: ReadRuntime, domain_id: str, revision: RevisionRow
) -> Schema:
    """Schema of a Revision, read from its tree and cached by schema partition hash."""
    settings = runtime.store_settings
    nodes = NodeRepository(
        session,
        domain_id,
        writes=settings.writes,
        cache=runtime.store.node_cache,
        batch=settings.nodes.prefetch_batch,
    )
    tree = await nodes.load_tree(revision.root)
    partition = tree.partitions.get(PARTITION_SCHEMA)
    if partition is not None:
        cached = runtime.schemas.get(domain_id, partition.root)
        if cached is not None:
            return cached
    schema, schema_hash = await load_schema(session, domain_id, tree, runtime=runtime.store)
    runtime.schemas.put(domain_id, schema_hash, schema)
    return schema
