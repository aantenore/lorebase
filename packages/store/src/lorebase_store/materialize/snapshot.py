"""Snapshots: the materialized rows of a past Revision, for reads at it (design 6.12).

A read at a Revision other than a branch head is served from the tree; filters, ordering,
includes, aggregates, Link summaries, traversal, search, history and health need materialized
rows. A snapshot provides them: a ref of kind `snapshot` named after the Revision
(`snapshot_name`), whose rows `materialize_revision` writes like for a branch rebuilt at that
Revision (content, history of its first-parent chain, lineage, derived values, path Links and
search vectors), without per-Attribute indexes and without a ref log entry. Branch listings,
bundles, embeddings and index plans ignore snapshot refs.

Snapshots are shared by every read of the Revision in the Domain. Readers take a shared
transaction-scoped advisory lock on a snapshot before using it (`hold_snapshot`); building
one (`build_snapshot`, serialized per Revision by the ref lock) first drops the oldest
snapshots beyond `keep` that no reader holds, through `delete_branch` (large ones are deleted
by the background job).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.tree import Tree
from lorebase_store.materialize.background import delete_branch
from lorebase_store.materialize.rebuild import materialize_revision
from lorebase_store.materialize.rows import PartitionType, parse_partition
from lorebase_store.models.enums import Materialization, RefKind
from lorebase_store.repositories.refs import REFS, RefRepository
from lorebase_store.repositories.revisions import RevisionRow
from lorebase_store.runtime import StoreRuntime

SNAPSHOT_PREFIX = "~snapshot/"
"""Prefix of snapshot ref names; `~` is not allowed in branch or tag names."""

SNAPSHOT_CREATOR = "lorebase:time-travel"
"""`created_by` of snapshot refs."""

_USE_KEY_SCOPE = b"lb1\x00snapshot-use\x00"


class SnapshotBusyError(Exception):
    """The snapshot of the Revision is being deleted; it can be built again once it is gone."""


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


async def hold_snapshot(session: AsyncSession, domain_id: str, revision: bytes) -> bool:
    """Take the shared lock of the snapshot of `revision` for the session's transaction (it
    is not dropped while held) and say whether the snapshot exists. Works in read-only
    transactions."""
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


async def build_snapshot(
    session: AsyncSession,
    domain_id: str,
    revision: RevisionRow,
    *,
    keep: int,
    runtime: StoreRuntime,
    clock: Callable[[], datetime] | None = None,
) -> bool:
    """Create the snapshot of `revision` in the caller's transaction (scoped to the Domain);
    `False` when it already exists. Snapshots beyond `keep` (this one included) are dropped
    first, oldest first, unless a reader holds them. Raises `SnapshotBusyError` while an
    earlier snapshot of the Revision is still being deleted."""
    now = (clock or (lambda: datetime.now(UTC)))()
    name = snapshot_name(revision.id)
    refs = RefRepository(session, domain_id)
    await refs.lock(name, timeout_ms=runtime.settings.commit.lock_timeout_ms)
    if await refs.get(name) is not None:
        return False
    if await refs.get_deleting(name) is not None:
        raise SnapshotBusyError(f"the snapshot {name!r} of Domain {domain_id!r} is being deleted")
    await _drop_oldest(session, domain_id, keep - 1, runtime=runtime, now=now)
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
    await materialize_revision(session, domain_id, name, revision, runtime=runtime, indexes=False)
    return True


async def _drop_oldest(
    session: AsyncSession, domain_id: str, keep: int, *, runtime: StoreRuntime, now: datetime
) -> None:
    """Delete the snapshots of the Domain beyond the `keep` newest that no reader holds."""
    names = (
        await session.scalars(
            select(REFS.c.name)
            .where(
                REFS.c.domain_id == domain_id,
                REFS.c.kind == RefKind.SNAPSHOT,
                REFS.c.deleted_at.is_(None),
            )
            .order_by(REFS.c.created_at.desc(), REFS.c.name)
        )
    ).all()
    for name in names[max(keep, 0) :]:
        free = await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _use_key(domain_id, name)}
        )
        if free:
            await delete_branch(session, domain_id, name, runtime=runtime, clock=lambda: now)
