"""Branch creation (design 6.5, 6.11 modes `full` and `overlay`).

Creating the ref is O(1). The source branch is locked meanwhile, so no commit moves it.

- `overlay`: the branch stores no rows yet and reads the source's rows as of its head
  (`materialize.overlay`); only the compiled schema row is copied. Available when the
  branch starts at the head of a fully materialized source that has fewer than
  `store.materialize.overlay_max_per_branch` overlays; otherwise the branch is a copy.
- `copy`: the materialized head is copied from the source branch with `INSERT ... SELECT`
  when the branch starts at the source head (an overlay source gives its resolved rows);
  otherwise it is rebuilt from the tree of the start Revision. The branch then gets its
  per-Attribute indexes. When an overlay is possible and the source holds more than
  `store.materialize.async_copy_min_rows` rows, the branch is created as an overlay and a
  `store.materialize` job copies its rows in batches (`materialize.background`):
  `BranchCreation.pending` is then true. A copy of that size that cannot start as an
  overlay (an overlay source, another start point, the overlay cap reached) is created
  `building`: the same job writes its rows from the tree of its start Revision in batches
  (`pending` too); meanwhile reads of its head are served from the tree and commits wait.

Without an explicit mode, `store.materialize.branch_mode` decides, except for names starting
with one of `overlay_branch_prefixes`, which default to overlays.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.tree import PARTITION_SCHEMA
from lorebase_store.commit.schema import load_compiled
from lorebase_store.indexes import BranchIndexes
from lorebase_store.materialize.background import queue_build, queue_materialization
from lorebase_store.materialize.overlay import wants_overlay
from lorebase_store.materialize.rebuild import materialize_revision
from lorebase_store.models.enums import Materialization, RefKind
from lorebase_store.nodestore import NodeRepository
from lorebase_store.repositories.domains import DomainRepository
from lorebase_store.repositories.materialized import MaterializedRepository
from lorebase_store.repositories.refs import RefRepository, RefRow
from lorebase_store.repositories.revisions import RevisionRepository, RevisionRow
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


class BranchDeletingError(Exception):
    """A branch with that name is being deleted in the background; the name is free again
    once its rows are gone."""


@dataclass(frozen=True, slots=True)
class BranchCreation:
    ref: RefRow
    mode: MaterializationMode
    pending: bool = False
    """A copy whose rows a background job is still copying (the ref is an overlay)."""


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
    if await refs.get_deleting(name) is not None:
        raise BranchDeletingError(
            f"branch {name!r} of Domain {domain_id!r} is being deleted; retry later"
        )
    source_name = source or (await DomainRepository(session).require(domain_id)).default_branch
    await refs.lock(source_name, timeout_ms=runtime.settings.commit.lock_timeout_ms)
    source_ref = await refs.require(source_name)
    target = start or source_ref.head
    head = await RevisionRepository(session, domain_id).require(target)
    source_rows = MaterializedRepository(
        session, domain_id, source_name, writes=runtime.settings.writes
    )
    # A source still being built has no complete rows to copy: its tree is read instead.
    building = source_ref.materialization is Materialization.BUILDING
    source_schema = None if building else await source_rows.schema()
    at_head = source_ref.head == head.id and source_schema is not None
    settings = runtime.settings.materialize
    overlay_possible = (
        at_head
        and source_ref.kind is RefKind.BRANCH
        and source_ref.materialization is Materialization.FULL
        and len(await refs.overlays_of(source_name)) < settings.overlay_max_per_branch
    )
    background = (
        wanted is BranchMode.COPY
        and overlay_possible
        and await source_rows.own_rows(limit=settings.async_copy_min_rows)
        > settings.async_copy_min_rows
    )
    if (wanted is BranchMode.OVERLAY and overlay_possible) or background:
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
        # Queued: the overlay half of its reads uses them once the job worker built them.
        await BranchIndexes(
            session,
            domain_id,
            name,
            settings=runtime.settings.indexes,
            maintenance=runtime.index_maintenance,
        ).sync(load_compiled(source_schema.compiled))
        if background:
            await queue_materialization(session, domain_id, ref, at=now())
            return BranchCreation(ref, MaterializationMode.COPY, pending=True)
        return BranchCreation(ref, MaterializationMode.OVERLAY)
    if await _large_copy(session, domain_id, source_rows, head, at_head=at_head, runtime=runtime):
        ref = await refs.create(
            name,
            head.id,
            created_at=now(),
            created_by=created_by,
            kind=RefKind.BRANCH,
            materialization=Materialization.BUILDING,
            reason=BRANCH_CREATE_REASON,
        )
        await queue_build(session, domain_id, name, source=source_name, head=head, at=now())
        mode = MaterializationMode.COPY if at_head else MaterializationMode.REBUILD
        return BranchCreation(ref, mode, pending=True)
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


async def _large_copy(
    session: AsyncSession,
    domain_id: str,
    source_rows: MaterializedRepository,
    head: RevisionRow,
    *,
    at_head: bool,
    runtime: StoreRuntime,
) -> bool:
    """Whether a copy that cannot start as an overlay holds more than
    `async_copy_min_rows` rows: the rows the source reads at its head (an overlay's own rows
    and its base's, an upper bound), or the entries of the start Revision's tree."""
    limit = runtime.settings.materialize.async_copy_min_rows
    if at_head:
        layout = await source_rows.layout()
        total = await source_rows.own_rows(limit=limit)
        if layout.base is not None and total <= limit:
            base_rows = MaterializedRepository(
                session, domain_id, layout.base, writes=runtime.settings.writes
            )
            total += await base_rows.own_rows(limit=limit)
        return total > limit
    settings = runtime.settings
    nodes = NodeRepository(
        session, domain_id, writes=settings.writes, batch=settings.nodes.prefetch_batch
    )
    tree = await nodes.load_tree(head.root)
    total = sum(ref.count for name, ref in tree.partitions.items() if name != PARTITION_SCHEMA)
    return total > limit
