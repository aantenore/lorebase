"""Materialized rows for reads at a past Revision (design 6.12).

Key reads and listings ordered by key at a Revision other than the branch head read the tree.
Reads that need materialized rows (filters, ordering, includes, aggregates, Link and lineage
summaries, traversal, neighborhoods, search, Record history, health, context packs and
explanations) read a snapshot of the Revision instead (`lorebase_store.materialize.snapshot`)
with the statements of a head read, so they answer what the head answered when the Revision
was the head. Callers validate and authorize the request before asking for the snapshot.

The snapshot is built on first use and shared by later reads of the Revision. The read holds
the snapshot's shared lock from before the build, so nothing drops it between the build and
the read. Building is bounded (`read.time_travel`):

- Revisions holding more than `snapshot_max_entries` Records and Links, or more than
  `snapshot_max_history_revisions` Revisions of history, are refused (422 `time_travel`);
- each principal may start `snapshot_builds_per_minute` builds (burst `snapshot_build_burst`)
  in this process, then gets 429 with Retry-After;
- at most `snapshot_max_concurrent_builds` builds run in this process, each on a second
  database connection; builds of one Domain run one at a time; a read waits
  `snapshot_build_wait_ms` for its turn, then gets 503 with Retry-After;
- unused snapshots older than `snapshot_min_age_s` are dropped in a short transaction before
  a build; a Domain holding `snapshot_max_per_domain` snapshots none of which can go answers
  503 with Retry-After.

Authorization and masking are those of the read point: a snapshot point differs only in where
its rows come from (`ReadPoint.layout`).
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_keeper.errors import NOT_FOUND, RATE_LIMITED, UNAVAILABLE, ProblemError
from lorebase_keeper.ratelimit.bucket import BucketSpec
from lorebase_keeper.reading.points import ReadPoint
from lorebase_keeper.reading.problems import snapshot_refused
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.reading.tree import TreeReader
from lorebase_store.layout import BranchLayout
from lorebase_store.materialize.snapshot import (
    SnapshotBusyError,
    build_snapshot,
    drop_unused,
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
    if settings.snapshot_max_entries == 0 or entries > settings.snapshot_max_entries:
        raise snapshot_refused(
            what,
            f"this one holds {entries} Records and Links, more than "
            f"read.time_travel.snapshot_max_entries ({settings.snapshot_max_entries})",
        )
    history = revision.generation + 1
    if history > settings.snapshot_max_history_revisions:
        raise snapshot_refused(
            what,
            f"its history holds up to {history} Revisions, more than "
            f"read.time_travel.snapshot_max_history_revisions "
            f"({settings.snapshot_max_history_revisions})",
        )
    # Held from here on: a snapshot built below cannot be dropped before this read ends.
    if not await hold_snapshot(session, domain_id, revision.id):
        await _build(runtime, point)
        if not await hold_snapshot(session, domain_id, revision.id):
            raise _busy(runtime)
    layout = BranchLayout(domain_id, snapshot_name(revision.id))
    return replace(point, layout=layout, snapshot=True)


async def _build(runtime: ReadRuntime, point: ReadPoint) -> None:
    """Build the snapshot of the point's Revision within the build bounds (module doc)."""
    settings = runtime.read.time_travel
    spec = BucketSpec(
        capacity=settings.snapshot_build_burst,
        refill_per_s=settings.snapshot_builds_per_minute / 60,
    )
    principal = point.scope.guard.principal.id
    decision = await runtime.snapshot_rates.take(f"{principal}\x00snapshot", spec)
    if not decision.allowed:
        raise ProblemError(
            RATE_LIMITED,
            "Too many snapshot builds for reads at past Revisions; retry later.",
            headers={"Retry-After": str(decision.retry_after_s)},
        )
    wait_ms = settings.snapshot_build_wait_ms
    try:
        await asyncio.wait_for(runtime.snapshot_builds.acquire(), timeout=wait_ms / 1000)
    except TimeoutError:
        raise _busy(runtime) from None
    domain_id = point.domain.id
    try:
        async with runtime.sessions() as writer:
            async with writer.begin():
                await scope_to_domain(writer, domain_id)
                await drop_unused(
                    writer,
                    domain_id,
                    keep=settings.snapshot_max_per_domain - 1,
                    min_age_s=settings.snapshot_min_age_s,
                    runtime=runtime.store,
                    lock_timeout_ms=wait_ms,
                )
            async with writer.begin():
                await scope_to_domain(writer, domain_id)
                await build_snapshot(
                    writer,
                    domain_id,
                    point.revision,
                    max_snapshots=settings.snapshot_max_per_domain,
                    runtime=runtime.store,
                    lock_timeout_ms=wait_ms,
                )
    except (SnapshotBusyError, LockTimeoutError):
        raise _busy(runtime) from None
    except LookupError:
        raise ProblemError(NOT_FOUND, f"Revision {point.revision_hex} not found.") from None
    finally:
        runtime.snapshot_builds.release()


def _busy(runtime: ReadRuntime) -> ProblemError:
    retry = max(1, math.ceil(runtime.read.time_travel.snapshot_build_wait_ms / 1000))
    return ProblemError(
        UNAVAILABLE,
        "No snapshot of this Revision can be built right now (other builds or snapshots in "
        "use); retry shortly.",
        headers={"Retry-After": str(retry)},
    )
