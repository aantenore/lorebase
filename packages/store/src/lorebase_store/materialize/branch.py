"""Branch creation with full materialization (design 6.5, 6.11 mode `full`).

Creating the ref is O(1). Its materialized head is copied from the source branch with
`INSERT ... SELECT` when the branch starts at the source head (the source branch is locked
meanwhile, so no commit moves it during the copy); otherwise it is rebuilt from the tree of
the start Revision.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.materialize.rebuild import materialize_revision
from lorebase_store.models.enums import Materialization, RefKind
from lorebase_store.repositories.domains import DomainRepository
from lorebase_store.repositories.materialized import MaterializedRepository
from lorebase_store.repositories.refs import RefRepository, RefRow
from lorebase_store.repositories.revisions import RevisionRepository
from lorebase_store.repositories.scope import scope_to_domain
from lorebase_store.runtime import StoreRuntime, default_runtime

BRANCH_CREATE_REASON = "branch.create"


class MaterializationMode(StrEnum):
    """How a new branch got its materialized head."""

    COPY = "copy"
    REBUILD = "rebuild"


class BranchExistsError(Exception):
    """A ref with that name already exists."""


@dataclass(frozen=True, slots=True)
class BranchCreation:
    ref: RefRow
    mode: MaterializationMode


async def create_branch(
    session: AsyncSession,
    domain_id: str,
    name: str,
    *,
    created_by: str,
    source: str | None = None,
    start: bytes | None = None,
    runtime: StoreRuntime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> BranchCreation:
    """Create branch `name` at `start` (default: the head of `source`, itself defaulting to
    the Domain's default branch) with a full materialized head.

    Runs in the caller's transaction when one is open, otherwise in its own.
    """
    runtime = runtime or default_runtime()
    now = clock or (lambda: datetime.now(UTC))
    if session.in_transaction():
        return await _create(session, domain_id, name, created_by, source, start, runtime, now)
    async with session.begin():
        return await _create(session, domain_id, name, created_by, source, start, runtime, now)


async def _create(
    session: AsyncSession,
    domain_id: str,
    name: str,
    created_by: str,
    source: str | None,
    start: bytes | None,
    runtime: StoreRuntime,
    now: Callable[[], datetime],
) -> BranchCreation:
    await scope_to_domain(session, domain_id)
    refs = RefRepository(session, domain_id)
    if await refs.get(name) is not None:
        raise BranchExistsError(f"ref {name!r} already exists in Domain {domain_id!r}")
    source_name = source or (await DomainRepository(session).require(domain_id)).default_branch
    await refs.lock(source_name, timeout_ms=runtime.settings.commit.lock_timeout_ms)
    source_ref = await refs.require(source_name)
    target = start or source_ref.head
    head = await RevisionRepository(session, domain_id).require(target)
    ref = await refs.create(
        name,
        head.id,
        created_at=now(),
        created_by=created_by,
        kind=RefKind.BRANCH,
        materialization=Materialization.FULL,
        reason=BRANCH_CREATE_REASON,
    )
    repository = MaterializedRepository(session, domain_id, name, writes=runtime.settings.writes)
    source_rows = MaterializedRepository(
        session, domain_id, source_name, writes=runtime.settings.writes
    )
    if source_ref.head == head.id and await source_rows.schema() is not None:
        await repository.copy_from(source_name)
        return BranchCreation(ref, MaterializationMode.COPY)
    await materialize_revision(session, domain_id, name, head, runtime=runtime)
    return BranchCreation(ref, MaterializationMode.REBUILD)
