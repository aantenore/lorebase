"""Snapshots: the materialized rows of a past Revision, for reads at it (design 6.12).

A read at a Revision other than a branch head is served from the tree; filters, ordering,
includes, aggregates, Link summaries, traversal, search, history and health need materialized
rows. A snapshot provides them: a ref of kind `snapshot` named after the Revision
(`snapshot_name`), whose rows `materialize_revision` writes like for a branch rebuilt at that
Revision (content, history of its first-parent chain, lineage, derived values, path Links and
search vectors) with the sensitivity tags of the Revision's own schema, without per-Attribute
indexes and without a ref log entry. Branch listings, bundles, embeddings, index plans and the
garbage collector's branch guard ignore snapshot refs; the collector keeps their Revisions.

Snapshots are shared by every read of the Revision in the Domain:

- Readers take a shared transaction-scoped advisory lock on a snapshot before using it
  (`hold_snapshot`, also before it exists), so it is not dropped while they read it.
- `drop_unused` (its own short transaction) deletes the oldest snapshots beyond `keep` that no
  reader holds and that are older than a grace period, through `delete_branch` (large ones are
  deleted by the background job).
- `build_snapshot` creates one under the Domain's snapshot lock, which serializes the builds
  of a Domain and their count against the cap: refs being deleted count too, and a full Domain
  raises `SnapshotBusyError` instead of exceeding it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.tree import Tree
from lorebase_store.materialize.background import delete_branch
from lorebase_store.materialize.rebuild import materialize_revision
from lorebase_store.materialize.rows import PartitionType, parse_partition
from lorebase_store.models.enums import Materialization, RefKind
from lorebase_store.repositories.refs import REFS, RefRepository
from lorebase_store.repositories.revisions import RevisionRepository, RevisionRow
from lorebase_store.runtime import StoreRuntime

SNAPSHOT_PREFIX = "~snapshot/"
"""Prefix of snapshot ref names; branch and tag names may not start with it."""

SNAPSHOT_CREATOR = "lorebase:time-travel"
"""`created_by` of snapshot refs."""

_DOMAIN_LOCK = "~snapshot"
"""Ref lock name serializing the snapshot builds and drops of a Domain."""

_USE_KEY_SCOPE = b"lb1\x00snapshot-use\x00"

Clock = Callable[[], datetime]


class SnapshotBusyError(Exception):
    """No snapshot can be built now: the Domain holds its maximum (in use, recent or being
    deleted), or the snapshot of the Revision is still being deleted."""


def snapshot_name(revision: bytes) -> str:
    """Ref name of the snapshot of `revision`."""
    return SNAPSHOT_PREFIX + revision.hex()


def snapshot_entries(tree: Tree) -> int:
    """Records and Links a snapshot of `tree` holds (O(1), from the partition counts)."""
    return sum(
        ref.count
        for partition, ref in tree.partitions.items()
        if parse_partition(partition).type is not PartitionType.SCHEMA
    )


def _use_key(domain_id: str, name: str) -> int:
    digest = hashlib.sha256(
        _USE_KEY_SCOPE + domain_id.encode("utf-8") + b"\x00" + name.encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _now() -> datetime:
    return datetime.now(UTC)


async def hold_snapshot(session: AsyncSession, domain_id: str, revision: bytes) -> bool:
    """Take the shared lock of the snapshot of `revision` for the session's transaction (it
    is not dropped while held, even when it is built later) and say whether the snapshot
    exists. Works in read-only transactions."""
    name = snapshot_name(revision)
    await session.execute(
        text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": _use_key(domain_id, name)}
    )
    found = await session.scalar(
        select(REFS.c.name).where(
            REFS.c.domain_id == domain_id,
            REFS.c.name == name,
            REFS.c.kind == RefKind.SNAPSHOT,
            REFS.c.deleted_at.is_(None),
        )
    )
    return found is not None


async def drop_unused(
    session: AsyncSession,
    domain_id: str,
    *,
    keep: int,
    min_age_s: float,
    runtime: StoreRuntime,
    lock_timeout_ms: int,
    clock: Clock | None = None,
) -> int:
    """Delete the snapshots of the Domain beyond the `keep` newest that no reader holds and
    that were built at least `min_age_s` ago, in the caller's (short) transaction; returns how
    many were deleted or scheduled for deletion."""
    now = (clock or _now)()
    await RefRepository(session, domain_id).lock(_DOMAIN_LOCK, timeout_ms=lock_timeout_ms)
    names = (
        await session.scalars(
            select(REFS.c.name)
            .where(
                REFS.c.domain_id == domain_id,
                REFS.c.kind == RefKind.SNAPSHOT,
                REFS.c.deleted_at.is_(None),
                REFS.c.created_at <= now - timedelta(seconds=min_age_s),
            )
            .order_by(REFS.c.created_at.desc(), REFS.c.name)
        )
    ).all()
    young = await _count(session, domain_id, live=True) - len(names)
    dropped = 0
    for name in names[max(keep - young, 0) :]:
        free = await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _use_key(domain_id, name)}
        )
        if free:
            await delete_branch(session, domain_id, name, runtime=runtime, clock=lambda: now)
            dropped += 1
    return dropped


async def build_snapshot(
    session: AsyncSession,
    domain_id: str,
    revision: RevisionRow,
    *,
    max_snapshots: int,
    runtime: StoreRuntime,
    lock_timeout_ms: int,
    clock: Clock | None = None,
) -> bool:
    """Create the snapshot of `revision` in the caller's transaction (scoped to the Domain);
    `False` when it already exists. Raises `SnapshotBusyError` when the Domain already holds
    `max_snapshots` snapshots (being deleted included) or this one is being deleted, and
    `LockTimeoutError` when another build of the Domain holds the lock longer than
    `lock_timeout_ms`."""
    now = (clock or _now)()
    name = snapshot_name(revision.id)
    refs = RefRepository(session, domain_id)
    await refs.lock(_DOMAIN_LOCK, timeout_ms=lock_timeout_ms)
    if await refs.get(name) is not None:
        return False
    if await refs.get_deleting(name) is not None:
        raise SnapshotBusyError(f"the snapshot {name!r} of Domain {domain_id!r} is being deleted")
    held = await _count(session, domain_id, live=False)
    if held >= max_snapshots:
        raise SnapshotBusyError(
            f"Domain {domain_id!r} holds {held} snapshots, the most allowed ({max_snapshots})"
        )
    await refs.create(
        name,
        revision.id,
        created_at=now,
        created_by=SNAPSHOT_CREATOR,
        kind=RefKind.SNAPSHOT,
        materialization=Materialization.FULL,
        reason="snapshot",
        log=False,
    )
    # The ref now roots the Revision for the garbage collector; one that removed the Revision
    # before (it was unreachable) leaves nothing to build.
    if await RevisionRepository(session, domain_id).get(revision.id) is None:
        raise LookupError(f"Revision {revision.id.hex()} of Domain {domain_id!r} is gone")
    await materialize_revision(
        session,
        domain_id,
        name,
        revision,
        runtime=runtime,
        indexes=False,
        default_branch_tags=False,
    )
    return True


async def _count(session: AsyncSession, domain_id: str, *, live: bool) -> int:
    """Snapshot refs of the Domain: live ones only, or those being deleted too."""
    query = select(func.count()).where(
        REFS.c.domain_id == domain_id, REFS.c.kind == RefKind.SNAPSHOT
    )
    if live:
        query = query.where(REFS.c.deleted_at.is_(None))
    return int(await session.scalar(query) or 0)
