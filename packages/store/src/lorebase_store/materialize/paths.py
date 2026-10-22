"""Materialized path Derivations (design 5.8): the Links of a virtual Relation in `mat_links`.

A path Derivation marked `materialized` keeps its Links in `mat_links` under the Derivation
id as `relation_id` (`DERIVED_LINK_PREFIX`), with no values and a digest of the pair as
`object_hash`; they are not tree objects, so they take no part in hashing, diff, merge or the
tree comparison of verification. Readers then use them like stored Links (reverse index
included); paths that are not materialized are composed at read time
(`lorebase_keeper.reading.compiler`). Both follow `lorebase_core.model.paths`: every Link of
the first step, at most `fanout` Links (smallest far keys) per intermediate Record later.

`PathLinks` keeps them current in the commit transaction, after derived values:

- `recompute_all` rewrites every materialized path (rebuilds, schema changes); the fan-out
  used is recorded in `mat_schema.path_fanout` (`record_fanout`);
- `fanout_stale` tells whether the recorded fan-out differs from `store.traverse.path_fanout`
  while materialized paths or derived values reading a path depend on it: commits check it
  before their incremental update and then recompute both whole, and `refresh_stale_paths`
  does the same for every branch (the job workers run it in the background when they start,
  `run_path_fanout_sweep`; branches with nothing depending on the fan-out only record it);
- `recompute` rewrites the pairs of the start Records a changeset can affect. A Link of step
  `i` that changed (for a foreign-key step: a `from` Record written or removed) marks its
  near Record at level `i`; the start Records reaching a marked Record, found by walking the
  steps backwards without bound, are recomputed from scratch. Above
  `store.materialize.derivation_max_incremental_records` start Records the path is rewritten
  whole.

Writes go through `MaterializedRepository`, so overlays copy and tombstone path Links like
any other row. With `write=False` nothing is written and `verify` compares the pairs it
computes (`expected`) with the stored ones.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.expr.sensitivity import with_tags
from lorebase_core.model import PathRelation, Schema
from lorebase_store.auth.scope import rls_scope
from lorebase_store.bulk import chunks
from lorebase_store.materialize.derived import AppliedChanges, DerivedValues
from lorebase_store.materialize.links import path_affected, path_pairs_from
from lorebase_store.materialize.rows import search_weights
from lorebase_store.materialize.sensitivity import reference_tags
from lorebase_store.repositories.materializations import BranchMaterializationRepository
from lorebase_store.repositories.materialized import LinkRow, MaterializedRepository
from lorebase_store.repositories.refs import RefRepository
from lorebase_store.repositories.revisions import RevisionRepository
from lorebase_store.repositories.scope import scope_to_domain
from lorebase_store.runtime import StoreRuntime

logger = logging.getLogger("lorebase.store.paths")

Pair = tuple[str, str]

# Branches with a path Derivation whose recorded fan-out differs from the setting.
_STALE = text(
    "SELECT domain_id, branch FROM mat_schema "
    "WHERE path_fanout IS DISTINCT FROM :fanout "
    "AND jsonb_path_exists(compiled, '$.derivations.*.definition ? (@.variant == \"path\")') "
    "ORDER BY domain_id, branch"
)


def path_link_hash(path_id: str, from_key: str, to_key: str) -> bytes:
    """`object_hash` of a materialized path Link: a digest of the pair, never an object."""
    return hashlib.sha256(f"lb1 path\0{path_id}\0{from_key}\0{to_key}".encode()).digest()


class PathLinks:
    """Keeps the materialized path Derivations of one schema on one branch current."""

    def __init__(
        self,
        repository: MaterializedRepository,
        schema: Schema,
        *,
        fanout: int,
        batch: int,
        max_incremental: int,
        write: bool = True,
    ) -> None:
        self.repository = repository
        self.fanout = fanout
        self.batch = batch
        self.max_incremental = max_incremental
        self.write = write
        self.paths = sorted(
            (p for p in schema.path_relations().values() if p.materialized), key=lambda p: p.id
        )
        self.expected: dict[str, set[Pair]] = {}

    # Entry points -----------------------------------------------------------------------------

    async def prune(self) -> None:
        """Drop the Links of path Derivations that are gone or no longer materialized."""
        keep = {path.id for path in self.paths}
        for path_id in sorted(await self.repository.link_counts(derived=True)):
            if path_id not in keep:
                pairs = [p async for p in self._stored_pairs(path_id)]
                await self.repository.delete_links(path_id, pairs)

    async def recompute_all(self) -> None:
        for path in self.paths:
            await self._rewrite(path)

    async def fanout_stale(self, derived: DerivedValues | None) -> bool:
        """Whether materialized paths, or derived values reading a path, were computed with
        another fan-out than `store.traverse.path_fanout` (or none was recorded yet)."""
        dependent = bool(self.paths) or (derived is not None and derived.reads_paths)
        return dependent and await self.repository.path_fanout() != self.fanout

    async def record_fanout(self) -> None:
        """Record the fan-out the branch's path Links and derived values now follow."""
        if self.write:
            await self.repository.set_path_fanout(self.fanout)

    async def refresh(self, path: PathRelation, starts: Sequence[str]) -> None:
        """Recompute the pairs of the start keys `starts` (a piece of `recompute_all` that a
        background job commits on its own)."""
        await self._refresh(path, starts)

    async def recompute(self, applied: AppliedChanges) -> None:
        for path in self.paths:
            starts = await self.affected(path, applied)
            if not starts:
                continue
            if len(starts) > self.max_incremental:
                await self._rewrite(path)
                continue
            for chunk in chunks(sorted(starts), self.batch):
                await self._refresh(path, chunk)

    # Affected start Records -------------------------------------------------------------------

    async def affected(self, path: PathRelation, applied: AppliedChanges) -> set[str]:
        """Start Records whose pairs a changeset can change (a superset)."""
        return await path_affected(self.repository, path.steps, applied)

    # Evaluation -------------------------------------------------------------------------------

    async def pairs_from(self, path: PathRelation, starts: Sequence[str]) -> dict[str, set[str]]:
        """Far keys reached from each start Record (`lorebase_core.model.paths`)."""
        return await path_pairs_from(self.repository, path, starts, self.fanout)

    async def _refresh(self, path: PathRelation, starts: Sequence[str]) -> None:
        """Recompute the pairs of `starts` and write the difference."""
        reached = await self.pairs_from(path, starts)
        wanted = {(start, far) for start, fars in reached.items() for far in fars}
        if not self.write:
            self.expected.setdefault(path.id, set()).update(wanted)
            return
        stored = {
            (near, far)
            for near, far, _ in await self.repository.linked_keys(
                path.id, near="from_key", keys=starts
            )
        }
        gone = sorted(stored - wanted)
        if gone:
            await self.repository.delete_links(path.id, gone)
        await self.repository.upsert_links(
            [
                LinkRow(path.id, f, t, {}, path_link_hash(path.id, f, t), {})
                for f, t in sorted(wanted - stored)
            ]
        )

    async def _rewrite(self, path: PathRelation) -> None:
        """Recompute every pair: from every Record of the start Kind and every start key
        stored (a start Kind changed by a schema change leaves old keys behind)."""
        starts: set[str] = {f async for f, _ in self._stored_pairs(path.id)}
        async for rows in self.repository.stream_record_hashes(
            path.relation.from_kind, batch=self.batch
        ):
            starts.update(key for key, _ in rows)
        for chunk in chunks(sorted(starts), self.batch):
            await self._refresh(path, chunk)

    async def _stored_pairs(self, path_id: str) -> AsyncIterator[Pair]:
        async for rows in self.repository.stream_links(path_id, batch=self.batch):
            for row in rows:
                yield row.from_key, row.to_key

    async def stored(self, path: PathRelation) -> set[Pair]:
        """The pairs stored for a path."""
        return {pair async for pair in self._stored_pairs(path.id)}

    async def compute_all(self, path: PathRelation) -> None:
        """Compute every pair of a path into `expected` (with `write=False`)."""
        await self._rewrite(path)


async def refresh_path_fanout(
    session: AsyncSession, domain_id: str, branch: str, *, runtime: StoreRuntime
) -> bool:
    """Rewrite the materialized paths of `branch`, and recompute its derived values when one
    reads a path, when they were computed with another fan-out; in the caller's transaction
    and under the branch lock. Returns whether it did. A branch whose background copy is
    queued or running is left alone (rewriting it would hold its lock for long and turn its
    path Links and derived values into own rows mid-copy): its next commit, or the next
    sweep, rewrites it."""
    settings = runtime.settings
    if not settings.materialize.derivations_enabled:
        return False
    await scope_to_domain(session, domain_id)
    refs = RefRepository(session, domain_id)
    await refs.lock(branch, timeout_ms=settings.commit.lock_timeout_ms)
    ref = await refs.require(branch)
    if await BranchMaterializationRepository(session, domain_id).active(branch):
        return False
    head = await RevisionRepository(session, domain_id).require(ref.head)
    repository = MaterializedRepository(
        session,
        domain_id,
        branch,
        writes=settings.writes,
        layout=ref.layout,
        generation=head.generation,
        lock_timeout_ms=settings.commit.lock_timeout_ms,
    )
    row = await repository.schema()
    if row is None:
        return False
    # Imported here: `lorebase_store.commit` imports this package (the commit pipeline
    # applies changesets), so a module-level import is circular when this package loads first.
    from lorebase_store.commit.schema import load_compiled

    schema = load_compiled(row.compiled)
    batch = settings.materialize.read_batch_rows
    max_incremental = settings.materialize.derivation_max_incremental_records
    fanout = settings.traverse.path_fanout
    derived = DerivedValues(
        repository,
        schema,
        limits=runtime.schema.expression_limits,
        now=head.created_at,
        batch=batch,
        max_incremental=max_incremental,
        fanout=fanout,
    )
    paths = PathLinks(
        repository, schema, fanout=fanout, batch=batch, max_incremental=max_incremental
    )
    stale = await paths.fanout_stale(derived)
    if stale:
        changed = await derived.recompute_all() if derived.reads_paths else {}
        await paths.recompute_all()
        tags = await reference_tags(session, domain_id, branch, runtime=runtime)
        tagged = with_tags(schema, tags)
        for kind_id, keys in sorted(changed.items()):
            if keys and derived.searchable(kind_id):
                await repository.refresh_search(
                    kind_id,
                    search_weights(tagged.kinds.get(kind_id)),
                    config=settings.materialize.text_search_config,
                    keys=sorted(keys),
                )
    await paths.record_fanout()  # also when nothing depends on it: not checked again
    return stale


async def refresh_stale_paths(
    sessions: async_sessionmaker[AsyncSession],
    *,
    runtime: StoreRuntime,
    stop: asyncio.Event | None = None,
) -> list[tuple[str, str]]:
    """Rewrite the materialized paths of every branch computed with another fan-out than
    `store.traverse.path_fanout`, one branch per transaction; returns the branches
    rewritten. A branch that fails (deleted or locked past `commit.lock_timeout_ms`
    meanwhile, or any other error) is logged and skipped: the next commit on it rewrites
    them. Stops between branches once `stop` is set. Job workers run it next to their job
    loop (`run_path_fanout_sweep`), not before it."""
    if not runtime.settings.materialize.derivations_enabled:
        return []
    async with sessions() as session, session.begin(), rls_scope(session, bypass=True):
        fanout = runtime.settings.traverse.path_fanout
        stale = [(d, b) for d, b in await session.execute(_STALE, {"fanout": fanout})]
    rewritten: list[tuple[str, str]] = []
    for domain_id, branch in stale:
        if stop is not None and stop.is_set():
            break
        try:
            async with sessions() as session, session.begin():
                if await refresh_path_fanout(session, domain_id, branch, runtime=runtime):
                    rewritten.append((domain_id, branch))
        except Exception as error:  # one branch must not stop the others
            logger.warning(
                "materialized paths not rewritten",
                extra={"domain": domain_id, "branch": branch, "error": type(error).__name__},
            )
    return rewritten


async def run_path_fanout_sweep(
    sessions: async_sessionmaker[AsyncSession],
    *,
    runtime: StoreRuntime,
    stop: asyncio.Event | None = None,
) -> None:
    """`refresh_stale_paths` as a background task of a job worker: never raises (an error
    listing the branches is logged), logs how many branches it rewrote."""
    try:
        rewritten = await refresh_stale_paths(sessions, runtime=runtime, stop=stop)
    except Exception as error:
        logger.warning("path fan-out sweep failed", extra={"error": type(error).__name__})
        return
    if rewritten:
        logger.info("materialized paths rewritten", extra={"branches": len(rewritten)})
