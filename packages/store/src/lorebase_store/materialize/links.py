"""Links of a branch by endpoint, for Relations and path Derivations (design 5.8).

`links_of` reads the Links of a Relation from near keys: stored Links, or the Records holding
the key of a foreign-key Relation. `path_links_of` reads the Links of a path Derivation the
same way, composed from the Links of its steps (`lorebase_core.model.paths`: every Link of
the first step, at most `fanout` Links, smallest far keys first, per intermediate Record
later). Read from its far end, a path is walked backwards and each intermediate Record is
checked to contribute the far key within its bound, so both directions give the same pairs.

`path_affected` lists the Records whose pairs a changeset can change, from the steps followed
from them: the start Records for the forward direction, the end Records for the inverse one
(`reverse_steps`, where a changed Link of an intermediate Record also marks the far keys it
can push in or out of that Record's bound).

All reads go through `MaterializedRepository`, so overlays read their resolved rows.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from lorebase_core.model import LinkDirection, PathRelation, Relation, RelationBacking
from lorebase_core.model.paths import PathHop, bounded
from lorebase_store.repositories.materialized import MaterializedRepository

if TYPE_CHECKING:
    from lorebase_store.materialize.derived import AppliedChanges


def reverse(direction: LinkDirection) -> LinkDirection:
    return LinkDirection.INVERSE if direction is LinkDirection.FORWARD else LinkDirection.FORWARD


def reverse_steps(steps: Sequence[PathHop]) -> tuple[PathHop, ...]:
    """The steps of a path walked from its far end."""
    return tuple(PathHop(step.relation, reverse(step.direction)) for step in reversed(steps))


async def links_of(
    repository: MaterializedRepository,
    relation: Relation,
    direction: LinkDirection,
    keys: Sequence[str],
    *,
    limit: int | None = None,
) -> dict[str, list[tuple[str, Any]]]:
    """`near key -> [(far key, link values)]` of `relation` followed in `direction` from
    `keys`: stored Links, or the Records holding the key of a foreign-key Relation; at most
    `limit` Links in all when set."""
    forward = direction is LinkDirection.FORWARD
    out: dict[str, list[tuple[str, Any]]] = defaultdict(list)
    if relation.backing is RelationBacking.FOREIGN_KEY:
        attribute = relation.foreign_key_attribute
        if attribute is None:
            return out
        pairs = await repository.foreign_key_links(
            relation.from_kind, attribute, keys=keys, by_target=not forward, limit=limit
        )
        for from_key, to_key in pairs:
            near, far = (from_key, to_key) if forward else (to_key, from_key)
            out[near].append((far, {}))
        return out
    found = await repository.linked_keys(
        relation.id, near="from_key" if forward else "to_key", keys=keys, limit=limit
    )
    for near, far, values in found:
        out[near].append((far, values))
    return out


class PathCapExceededError(Exception):
    """The first step of a path read with a cap (`path_links_of`) has that many Links."""


async def _fars(
    repository: MaterializedRepository,
    step: PathHop,
    keys: Sequence[str],
    cap: int | None = None,
) -> dict[str, list[str]]:
    """Far keys of `keys` along a step; with `cap`, raises `PathCapExceededError` when they have
    `cap` Links or more (reading no more than that)."""
    found = (
        await links_of(repository, step.relation, step.direction, keys, limit=cap) if keys else {}
    )
    if cap is not None and sum(len(items) for items in found.values()) >= cap:
        raise PathCapExceededError(cap)
    return {near: [far for far, _ in items] for near, items in found.items()}


async def path_pairs_from(
    repository: MaterializedRepository,
    path: PathRelation,
    starts: Sequence[str],
    fanout: int,
    cap: int | None = None,
) -> dict[str, set[str]]:
    """Far keys reached from each start Record (`lorebase_core.model.paths`). With `cap`,
    the first step is read up to `cap` Links (`_fars`)."""
    reached: dict[str, set[str]] = {start: {start} for start in starts}
    for index, step in enumerate(path.steps):
        found = await _fars(
            repository,
            step,
            sorted({k for keys in reached.values() for k in keys}),
            cap if index == 0 else None,
        )
        nexts = {
            near: (fars if index == 0 else bounded(fars, fanout)) for near, fars in found.items()
        }
        reached = {
            start: {far for near in keys for far in nexts.get(near, ())}
            for start, keys in reached.items()
        }
    return reached


async def path_pairs_to(
    repository: MaterializedRepository,
    path: PathRelation,
    ends: Sequence[str],
    fanout: int,
    cap: int | None = None,
) -> dict[str, set[str]]:
    """Start keys reaching each end Record: the steps walked backwards, keeping an
    intermediate Record only when the key it leads to is within its bound. With `cap`, the
    first step walked is read up to `cap` Links (`_fars`)."""
    reached: dict[str, set[str]] = {end: {end} for end in ends}
    last = len(path.steps) - 1
    for index in reversed(range(len(path.steps))):
        step = path.steps[index]
        frontier = sorted({k for keys in reached.values() for k in keys})
        back = await _fars(
            repository,
            PathHop(step.relation, reverse(step.direction)),
            frontier,
            cap if index == last else None,
        )
        if index > 0:
            nears = sorted({near for nears in back.values() for near in nears})
            allowed = {
                near: set(bounded(fars, fanout))
                for near, fars in (await _fars(repository, step, nears)).items()
            }
            back = {
                far: [near for near in nears if far in allowed.get(near, ())]
                for far, nears in back.items()
            }
        reached = {
            end: {near for far in keys for near in back.get(far, ())}
            for end, keys in reached.items()
        }
    return reached


async def path_links_of(
    repository: MaterializedRepository,
    path: PathRelation,
    direction: LinkDirection,
    keys: Sequence[str],
    fanout: int,
    *,
    cap: int | None = None,
) -> dict[str, list[tuple[str, Any]]]:
    """`near key -> [(far key, {})]` of a path Derivation followed in `direction` from
    `keys`, far keys in order (the shape of `links_of`). With `cap`, raises
    `PathCapExceededError` when the first step walked has `cap` Links or more, read no further."""
    if direction is LinkDirection.FORWARD:
        reached = await path_pairs_from(repository, path, keys, fanout, cap)
    else:
        reached = await path_pairs_to(repository, path, keys, fanout, cap)
    return {near: [(far, {}) for far in sorted(fars)] for near, fars in reached.items() if fars}


async def path_affected(
    repository: MaterializedRepository,
    steps: Sequence[PathHop],
    applied: AppliedChanges,
    *,
    fanout: int | None = None,
    inverse: bool = False,
) -> set[str]:
    """Records at the start of `steps` whose pairs a changeset can change (a superset): a
    Link of step `i` that changed marks its near Record at level `i`; walking the steps
    backwards without bound gives the start Records reaching a marked Record.

    `inverse` says `steps` are a path walked from its far end (`reverse_steps`): the bound
    of an intermediate Record then sits at the far side of a step, and a Link added or
    removed there can push another far key in or out of that Record's bounded set
    (`_displaced`), which marks it too."""
    starts: set[str] = set()
    for level, step in enumerate(steps):
        marked = await _marked(repository, step, applied)
        if inverse and fanout is not None and level < len(steps) - 1:
            marked |= await _displaced(repository, step, applied, fanout)
        for back in reversed(steps[:level]):
            if not marked:
                break
            found = await links_of(
                repository, back.relation, reverse(back.direction), sorted(marked)
            )
            marked = {far for items in found.values() for far, _ in items}
        starts |= marked
    return starts


async def _displaced(
    repository: MaterializedRepository, step: PathHop, applied: AppliedChanges, fanout: int
) -> set[str]:
    """Near Records of a step walked backwards whose membership in the bounded set of an
    intermediate Record (at the step's far side) a changeset can change. With `n` changed
    Links at an intermediate Record holding `c` Links now, its bounded set before and after
    the change lies within its `fanout + n` smallest far keys once `c + n` exceeds
    `fanout`: those are marked (the changed ones are marked by `_marked`)."""
    hop = PathHop(step.relation, reverse(step.direction))
    changes = await _changes(repository, hop, applied)
    if not changes:
        return set()
    current = await _fars(repository, hop, sorted(changes))
    displaced: set[str] = set()
    for near, count in changes.items():
        fars = sorted(set(current.get(near, ())))
        if len(fars) + count > fanout:
            displaced.update(fars[: fanout + count])
    return displaced


async def _marked(
    repository: MaterializedRepository, step: PathHop, applied: AppliedChanges
) -> set[str]:
    """Near Records of a step whose Links along it changed."""
    return set(await _changes(repository, step, applied))


async def _changes(
    repository: MaterializedRepository, step: PathHop, applied: AppliedChanges
) -> dict[str, int]:
    """Near Records of a step whose Links along it changed, with how many of their Links
    may have changed (an upper bound)."""
    relation = step.relation
    forward = step.direction is LinkDirection.FORWARD
    counts: dict[str, int] = defaultdict(int)
    for f, t in applied.links.get(relation.id, ()):
        counts[f if forward else t] += 1
    attribute = relation.foreign_key_attribute
    if relation.backing is not RelationBacking.FOREIGN_KEY or attribute is None:
        return counts
    source = relation.from_kind
    holders = set(applied.written.get(source, ())) | set(applied.removed.get(source, ()))
    if forward:
        for key in holders:
            counts[key] += 1
        return counts
    previous: Mapping[tuple[str, str], Mapping[str, Any] | None] = applied.previous
    for key in holders:  # targets before the change, then after it
        before = previous.get((source, key))
        if before is not None and isinstance(before.get(attribute), str):
            counts[before[attribute]] += 1
    if holders:
        now = await links_of(repository, relation, LinkDirection.FORWARD, sorted(holders))
        for items in now.values():
            for far, _ in items:
                counts[far] += 1
    return counts
