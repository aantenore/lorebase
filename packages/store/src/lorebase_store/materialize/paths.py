"""Materialized path Derivations (design 5.8): the Links of a virtual Relation in `mat_links`.

A path Derivation marked `materialized` keeps its Links in `mat_links` under the Derivation
id as `relation_id` (`DERIVED_LINK_PREFIX`), with no values and a digest of the pair as
`object_hash`; they are not tree objects, so they take no part in hashing, diff, merge or the
tree comparison of verification. Readers then use them like stored Links (reverse index
included); paths that are not materialized are composed at read time
(`lorebase_keeper.reading.compiler`). Both follow `lorebase_core.model.paths`: every Link of
the first step, at most `fanout` Links (smallest far keys) per intermediate Record later.

`PathLinks` keeps them current in the commit transaction, after derived values:

- `recompute_all` rewrites every materialized path (rebuilds, schema changes);
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

import hashlib
from collections.abc import AsyncIterator, Sequence

from lorebase_core.model import LinkDirection, PathRelation, RelationBacking, Schema
from lorebase_core.model.paths import PathHop, bounded
from lorebase_store.bulk import chunks
from lorebase_store.materialize.derived import AppliedChanges, links_of
from lorebase_store.repositories.materialized import LinkRow, MaterializedRepository

Pair = tuple[str, str]


def path_link_hash(path_id: str, from_key: str, to_key: str) -> bytes:
    """`object_hash` of a materialized path Link: a digest of the pair, never an object."""
    return hashlib.sha256(f"lb1 path\0{path_id}\0{from_key}\0{to_key}".encode()).digest()


def _reverse(direction: LinkDirection) -> LinkDirection:
    return LinkDirection.INVERSE if direction is LinkDirection.FORWARD else LinkDirection.FORWARD


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
        starts: set[str] = set()
        for level, step in enumerate(path.steps):
            marked = await self._marked(step, applied)
            for back in reversed(path.steps[:level]):
                if not marked:
                    break
                found = await links_of(
                    self.repository, back.relation, _reverse(back.direction), sorted(marked)
                )
                marked = {far for items in found.values() for far, _ in items}
            starts |= marked
        return starts

    async def _marked(self, step: PathHop, applied: AppliedChanges) -> set[str]:
        """Near Records of a step whose Links along it changed."""
        relation = step.relation
        forward = step.direction is LinkDirection.FORWARD
        marked = {f if forward else t for f, t in applied.links.get(relation.id, ())}
        attribute = relation.foreign_key_attribute
        if relation.backing is not RelationBacking.FOREIGN_KEY or attribute is None:
            return marked
        source = relation.from_kind
        holders = set(applied.written.get(source, ())) | set(applied.removed.get(source, ()))
        if forward:
            return marked | holders
        for key in holders:  # targets before the change, then after it
            before = applied.previous.get((source, key))
            if before is not None and isinstance(before.get(attribute), str):
                marked.add(before[attribute])
        if holders:
            now = await links_of(self.repository, relation, LinkDirection.FORWARD, sorted(holders))
            marked |= {far for items in now.values() for far, _ in items}
        return marked

    # Evaluation -------------------------------------------------------------------------------

    async def pairs_from(self, path: PathRelation, starts: Sequence[str]) -> dict[str, set[str]]:
        """Far keys reached from each start Record (`lorebase_core.model.paths`)."""
        reached: dict[str, set[str]] = {start: {start} for start in starts}
        for index, step in enumerate(path.steps):
            frontier = sorted({key for keys in reached.values() for key in keys})
            found = (
                await links_of(self.repository, step.relation, step.direction, frontier)
                if frontier
                else {}
            )
            nexts: dict[str, list[str]] = {
                near: (
                    [far for far, _ in items]
                    if index == 0
                    else bounded((far for far, _ in items), self.fanout)
                )
                for near, items in found.items()
            }
            reached = {
                start: {far for near in keys for far in nexts.get(near, ())}
                for start, keys in reached.items()
            }
        return reached

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
