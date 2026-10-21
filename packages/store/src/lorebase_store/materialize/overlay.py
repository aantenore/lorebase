"""Overlay branches (design 6.11 mode `overlay`, AC-16; store schema notes).

An overlay starts with no Record or Link rows: creating it inserts its ref (base branch, fork
Revision and generation) and a copy of the base's compiled schema row, and queues its
per-Attribute indexes (`lorebase_store.indexes`). Its commits write only the rows they change
(`MaterializedRepository`), and reads resolve it over the base (`lorebase_store.layout`).

An overlay is promoted to a full copy ("materialized"):
- when a commit leaves it with more than `store.materialize.overlay_max_rows` rows of its own,
  or when its base has moved more than `overlay_max_base_lag_revisions` generations past its
  fork point (checked by its own commits; a base commit that pushes an overlay past the lag
  queues a `store.materialize` job for it);
- before a commit or merge that changes its schema (whole-Kind rewrites and index changes
  belong to full branches);
- before its base is deleted or rebuilt (`promote_overlays_of`);
- on request (`promote`).

After an overlay is merged into its base, `refork` moves it to the base head and empties it,
which costs as much as creating it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_store.indexes import BranchIndexes
from lorebase_store.jobs import MATERIALIZE_JOB, JobRow, enqueue_job
from lorebase_store.layout import MAT_HISTORY, MAT_LINKS, MAT_RECORDS
from lorebase_store.models.enums import Materialization
from lorebase_store.repositories.materialized import MaterializedRepository
from lorebase_store.repositories.refs import RefMovedError, RefRepository, RefRow
from lorebase_store.repositories.revisions import RevisionRepository
from lorebase_store.repositories.scope import scope_to_domain
from lorebase_store.runtime import StoreRuntime, default_runtime
from lorebase_store.settings import MaterializeSettings

log = logging.getLogger(__name__)

REFORK_REASON = "branch.refork"


class OverlayError(Exception):
    """An overlay operation does not apply to the branch."""


def wants_overlay(name: str, settings: MaterializeSettings) -> bool:
    """Whether a branch created without an explicit mode defaults to an overlay."""
    if settings.branch_mode == "overlay":
        return True
    return any(name.startswith(prefix) for prefix in settings.overlay_branch_prefixes)


async def promote(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    *,
    runtime: StoreRuntime | None = None,
    locked: bool = False,
) -> bool:
    """Turn the overlay `branch` into a full copy; `False` when it is not an overlay.

    Takes the branch lock unless the caller holds it (`locked`). The head and ref version do
    not change, so a commit of the caller's transaction still moves the ref afterwards.
    """
    runtime = runtime or default_runtime()
    refs = RefRepository(session, domain_id)
    if not locked:
        await refs.lock(branch, timeout_ms=runtime.settings.commit.lock_timeout_ms)
    ref = await refs.get(branch)
    if ref is None or ref.materialization is not Materialization.OVERLAY:
        return False
    repository = MaterializedRepository(
        session, domain_id, branch, writes=runtime.settings.writes, layout=ref.layout
    )
    await repository.complete_overlay()
    await refs.set_materialization(branch, Materialization.FULL)
    schema = await repository.schema()
    if schema is not None:
        # Imported here: the commit package imports the materializer.
        from lorebase_store.commit.schema import load_compiled

        await BranchIndexes(
            session,
            domain_id,
            branch,
            settings=runtime.settings.indexes,
            maintenance=runtime.index_maintenance,
        ).sync(load_compiled(schema.compiled))
    log.info("promoted overlay branch %s of Domain %s to a full copy", branch, domain_id)
    return True


async def promote_overlays_of(
    session: AsyncSession, domain_id: str, base: str, *, runtime: StoreRuntime | None = None
) -> list[str]:
    """Promote every overlay based on `base` (before deleting or rebuilding it). The caller
    holds the lock of `base`; each overlay's lock is taken after it."""
    refs = RefRepository(session, domain_id)
    promoted = []
    for ref in await refs.overlays_of(base):
        if await promote(session, domain_id, ref.name, runtime=runtime):
            promoted.append(ref.name)
    return promoted


async def check_overlay(
    repository: MaterializedRepository, *, generation: int, runtime: StoreRuntime
) -> bool:
    """After a commit wrote `repository`'s branch at `generation`: promote it when it is an
    overlay over its row or lag limits; queue promotions of overlays based on it that the
    commit pushed past the lag limit. Returns whether the branch itself was promoted."""
    settings = runtime.settings.materialize
    layout = await repository.layout()
    session = repository.session
    if layout.overlay:
        assert layout.base is not None
        refs = RefRepository(session, layout.domain_id)
        base = await refs.get(layout.base)
        base_generation = None
        if base is not None:
            found = await RevisionRepository(session, layout.domain_id).generations([base.head])
            base_generation = found.get(base.head)
        lagging = (
            base_generation is not None
            and layout.base_generation is not None
            and base_generation - layout.base_generation > settings.overlay_max_base_lag_revisions
        )
        if lagging or await repository.own_rows(limit=settings.overlay_max_rows) > (
            settings.overlay_max_rows
        ):
            promoted = await promote(
                session, layout.domain_id, layout.branch, runtime=runtime, locked=True
            )
            repository.forget_layout()
            return promoted
        return False
    for name, base_generation in await repository.dependents():
        if generation - base_generation > settings.overlay_max_base_lag_revisions:
            await enqueue_job(
                session,
                MATERIALIZE_JOB,
                domain_id=layout.domain_id,
                payload={"branch": name},
                unique=True,
            )
    return False


async def refork(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    *,
    actor: str,
    base: str | None = None,
    runtime: StoreRuntime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> RefRow:
    """Move `branch` to the head of its base (default: its current base) as an empty overlay.

    The branch head must be an ancestor of the base head (merged into it), so the move is a
    fast-forward and loses nothing. Rows of the branch are dropped; the base is locked first,
    then the branch.
    """
    runtime = runtime or default_runtime()
    now = clock or (lambda: datetime.now(UTC))
    timeout = runtime.settings.commit.lock_timeout_ms
    await scope_to_domain(session, domain_id)
    refs = RefRepository(session, domain_id)
    ref = await refs.require(branch)
    base_name = base or ref.base_branch
    if base_name is None:
        raise OverlayError(f"branch {branch!r} has no base; name one")
    await refs.lock(base_name, timeout_ms=timeout)
    await refs.lock(branch, timeout_ms=timeout)
    ref = await refs.require(branch)
    base_ref = await refs.require(base_name)
    if base_ref.materialization is not Materialization.FULL:
        raise OverlayError(f"base branch {base_name!r} is not fully materialized")
    if await refs.overlays_of(branch):
        raise OverlayError(f"branch {branch!r} is the base of other overlays")
    revisions = RevisionRepository(session, domain_id)
    ahead, _ = await revisions.ahead_behind(ref.head, base_ref.head)
    if ahead:
        raise OverlayError(
            f"branch {branch!r} has {ahead} Revisions that {base_name!r} does not contain; "
            "merge it first"
        )
    head = await revisions.require(base_ref.head)
    writes = runtime.settings.writes
    for table in (MAT_RECORDS, MAT_LINKS, MAT_HISTORY):
        await session.execute(
            delete(table).where(table.c.domain_id == domain_id, table.c.branch == branch)
        )
    schema = await MaterializedRepository(session, domain_id, base_name, writes=writes).schema()
    if schema is not None:
        await MaterializedRepository(session, domain_id, branch, writes=writes).set_schema(schema)
    if ref.head != head.id:
        try:
            await refs.compare_and_swap(
                branch,
                expected_head=ref.head,
                expected_version=ref.version,
                new_head=head.id,
                actor=actor,
                at=now(),
                reason=REFORK_REASON,
            )
        except RefMovedError as error:
            raise OverlayError(f"branch {branch!r} moved meanwhile") from error
    await refs.set_materialization(
        branch,
        Materialization.OVERLAY,
        base_branch=base_name,
        base_revision=head.id,
        base_generation=head.generation,
    )
    if schema is not None:
        # Now an overlay: the indexes of the base's schema, none unique, queued.
        from lorebase_store.commit.schema import load_compiled

        await BranchIndexes(
            session,
            domain_id,
            branch,
            settings=runtime.settings.indexes,
            maintenance=runtime.index_maintenance,
        ).sync(load_compiled(schema.compiled))
    return await refs.require(branch)


async def run_materialize_job(
    engine: AsyncEngine, job: JobRow, *, runtime: StoreRuntime | None = None
) -> Mapping[str, Any]:
    """Job handler of `store.materialize`: promote the overlay named by the payload."""
    if job.domain_id is None:
        raise ValueError("a store.materialize job needs a Domain")
    branch = str(job.payload["branch"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await scope_to_domain(session, job.domain_id)
        promoted = await promote(session, job.domain_id, branch, runtime=runtime)
    return {"branch": branch, "promoted": promoted}
