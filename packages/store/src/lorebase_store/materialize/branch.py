"""Branch creation (design 6.5, 6.11 modes `full` and `overlay`).

Creating the ref is O(1). The source branch is locked meanwhile, so no commit moves it.

- `overlay`: the branch stores no rows yet and reads the source's rows as of its head
  (`materialize.overlay`); only the compiled schema row is copied. Available when the
  branch starts at the head of a fully materialized source that has fewer than
  `store.materialize.overlay_max_per_branch` overlays; otherwise the branch is a copy.
- `copy`: the materialized head is copied from the source branch with `INSERT ... SELECT`
  when the branch starts at the source head (an overlay source gives its resolved rows);
  otherwise it is rebuilt from the tree of the start Revision. The branch then gets its
  per-Attribute indexes.

Without an explicit mode, `store.materialize.branch_mode` decides, except for names starting
with one of `overlay_branch_prefixes`, which default to overlays.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.commit.schema import load_compiled
from lorebase_store.indexes import BranchIndexes
from lorebase_store.materialize.overlay import wants_overlay
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
    OVERLAY = "overlay"


class BranchMode(StrEnum):
    """The materialization a caller asks for when creating a branch."""

    COPY = "copy"
    OVERLAY = "overlay"


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
    mode: BranchMode | str | None = None,
    runtime: StoreRuntime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> BranchCreation:
    """Create branch `name` at `start` (default: the head of `source`, itself defaulting to
    the Domain's default branch), as an overlay or a full copy (`mode`, see module doc).

    Runs in the caller's transaction when one is open, otherwise in its own.
    """
    runtime = runtime or default_runtime()
    now = clock or (lambda: datetime.now(UTC))
    if mode is not None:
        wanted = BranchMode(mode)
    elif wants_overlay(name, runtime.settings.materialize):
        wanted = BranchMode.OVERLAY
    else:
        wanted = BranchMode.COPY
    args = (session, domain_id, name, created_by, source, start, wanted, runtime, now)
    if session.in_transaction():
        return await _create(*args)
    async with session.begin():
        return await _create(*args)


async def _create(
    session: AsyncSession,
    domain_id: str,
    name: str,
    created_by: str,
    source: str | None,
    start: bytes | None,
    wanted: BranchMode,
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
    source_rows = MaterializedRepository(
        session, domain_id, source_name, writes=runtime.settings.writes
    )
    source_schema = await source_rows.schema()
    at_head = source_ref.head == head.id and source_schema is not None
    if (
        wanted is BranchMode.OVERLAY
        and at_head
        and source_ref.kind is RefKind.BRANCH
        and source_ref.materialization is Materialization.FULL
        and len(await refs.overlays_of(source_name))
        < runtime.settings.materialize.overlay_max_per_branch
    ):
        assert source_schema is not None
        ref = await refs.create(
            name,
            head.id,
            created_at=now(),
            created_by=created_by,
            kind=RefKind.BRANCH,
            materialization=Materialization.OVERLAY,
            reason=BRANCH_CREATE_REASON,
            base_branch=source_name,
            base_revision=head.id,
            base_generation=head.generation,
        )
        await MaterializedRepository(
            session, domain_id, name, writes=runtime.settings.writes, layout=ref.layout
        ).set_schema(source_schema)
        return BranchCreation(ref, MaterializationMode.OVERLAY)
    ref = await refs.create(
        name,
        head.id,
        created_at=now(),
        created_by=created_by,
        kind=RefKind.BRANCH,
        materialization=Materialization.FULL,
        reason=BRANCH_CREATE_REASON,
    )
    repository = MaterializedRepository(
        session, domain_id, name, writes=runtime.settings.writes, layout=ref.layout
    )
    if at_head:
        assert source_schema is not None
        await repository.copy_from(source_name)
        await BranchIndexes(
            session,
            domain_id,
            name,
            settings=runtime.settings.indexes,
            maintenance=runtime.index_maintenance,
        ).sync(load_compiled(source_schema.compiled))
        return BranchCreation(ref, MaterializationMode.COPY)
    await materialize_revision(session, domain_id, name, head, runtime=runtime)
    return BranchCreation(ref, MaterializationMode.REBUILD)
