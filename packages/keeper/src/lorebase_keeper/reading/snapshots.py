"""Materialized rows for reads at a past Revision (design 6.12).

Key reads and listings ordered by key at a Revision other than the branch head read the tree.
Reads that need materialized rows (filters, ordering, includes, aggregates, Link and lineage
summaries, traversal, neighborhoods, search, Record history, health, context packs and
explanations) read a snapshot of the Revision instead (`lorebase_store.materialize.snapshot`)
with the statements of a head read, so they answer what the head answered when the Revision
was the head. The snapshot is built on first use, in its own transaction on the primary, and
shared by later reads of the Revision; Revisions holding more than
`read.time_travel.snapshot_max_entries` Records and Links are refused.

Authorization and masking are those of the read point: a snapshot point differs only in where
its rows come from (`ReadPoint.layout`).
"""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_keeper.errors import UNAVAILABLE, ProblemError
from lorebase_keeper.reading.points import ReadPoint
from lorebase_keeper.reading.problems import snapshot_refused
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.reading.tree import TreeReader
from lorebase_store.layout import BranchLayout
from lorebase_store.materialize.snapshot import (
    SnapshotBusyError,
    build_snapshot,
    hold_snapshot,
    snapshot_entries,
    snapshot_name,
)
from lorebase_store.repositories import scope_to_domain
from lorebase_store.repositories.refs import LockTimeoutError


async def with_rows(
    session: AsyncSession, runtime: ReadRuntime, point: ReadPoint, what: str
) -> ReadPoint:
    """`point` when materialized rows serve it, otherwise the same point read from a snapshot
    of its Revision (built when missing). `what` names the feature in a refusal."""
    if point.materialized:
        return point
    domain_id, revision = point.domain.id, point.revision
    settings = runtime.read.time_travel
    tree = await TreeReader(session, runtime, domain_id, revision.root).tree()
    entries = snapshot_entries(tree)
    if entries > settings.snapshot_max_entries:
        raise snapshot_refused(what, entries, settings.snapshot_max_entries)
    if not await hold_snapshot(session, domain_id, revision.id):
        try:
            async with runtime.sessions() as writer, writer.begin():
                await scope_to_domain(writer, domain_id)
                await build_snapshot(
                    writer,
                    domain_id,
                    revision,
                    keep=settings.snapshot_max_per_domain,
                    runtime=runtime.store,
                )
        except (SnapshotBusyError, LockTimeoutError):
            raise _busy() from None
        if not await hold_snapshot(session, domain_id, revision.id):
            raise _busy()
    layout = BranchLayout(domain_id, snapshot_name(revision.id))
    return replace(point, layout=layout, snapshot=True)


def _busy() -> ProblemError:
    return ProblemError(
        UNAVAILABLE, "The snapshot of this Revision is being built or replaced; retry shortly."
    )
