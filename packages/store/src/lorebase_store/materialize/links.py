"""Links of a branch by endpoint, for Relations and path Derivations (design 5.8).

`links_of` reads the Links of a Relation from near keys: stored Links, or the Records holding
the key of a foreign-key Relation. `path_links_of` reads the Links of a path Derivation the
same way, composed from the Links of its steps (`lorebase_core.model.paths`: every Link of
the first step, at most `fanout` Links, smallest far keys first, per intermediate Record
later). Read from its far end, a path is walked backwards and each intermediate Record is
checked to contribute the far key within its bound, so both directions give the same pairs.

`path_affected` lists the Records whose pairs a changeset can change, from the steps followed
from them: the start Records for the forward direction, the end Records for the inverse one
(`reverse_steps`).

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
) -> dict[str, list[tuple[str, Any]]]:
    """`near key -> [(far key, link values)]` of `relation` followed in `direction` from
    `keys`: stored Links, or the Records holding the key of a foreign-key Relation."""
    forward = direction is LinkDirection.FORWARD
    out: dict[str, list[tuple[str, Any]]] = defaultdict(list)
    if relation.backing is RelationBacking.FOREIGN_KEY:
        attribute = relation.foreign_key_attribute
        if attribute is None:
            return out
        pairs = await repository.foreign_key_links(
            relation.from_kind, attribute, keys=keys, by_target=not forward
        )
        for from_key, to_key in pairs:
            near, far = (from_key, to_key) if forward else (to_key, from_key)
            out[near].append((far, {}))
        return out
    found = await repository.linked_keys(
        relation.id, near="from_key" if forward else "to_key", keys=keys
    )
    for near, far, values in found:
        out[near].append((far, values))
    return out


async def _fars(
    repository: MaterializedRepository, step: PathHop, keys: Sequence[str]
) -> dict[str, list[str]]:
    found = await links_of(repository, step.relation, step.direction, keys) if keys else {}
    return {near: [far for far, _ in items] for near, items in found.items()}


async def path_pairs_from(
    repository: MaterializedRepository,
    path: PathRelation,
    starts: Sequence[str],
    fanout: int,
) -> dict[str, set[str]]:
    """Far keys reached from each start Record (`lorebase_core.model.paths`)."""
    reached: dict[str, set[str]] = {start: {start} for start in starts}
    for index, step in enumerate(path.steps):
        found = await _fars(
            repository, step, sorted({k for keys in reached.values() for k in keys})
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
) -> dict[str, set[str]]:
    """Start keys reaching each end Record: the steps walked backwards, keeping an
    intermediate Record only when the key it leads to is within its bound."""
    reached: dict[str, set[str]] = {end: {end} for end in ends}
    for index in reversed(range(len(path.steps))):
        step = path.steps[index]
        frontier = sorted({k for keys in reached.values() for k in keys})
        back = await _fars(repository, PathHop(step.relation, reverse(step.direction)), frontier)
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
) -> dict[str, list[tuple[str, Any]]]:
    """`near key -> [(far key, {})]` of a path Derivation followed in `direction` from
    `keys`, far keys in order (the shape of `links_of`)."""
    if direction is LinkDirection.FORWARD:
        reached = await path_pairs_from(repository, path, keys, fanout)
    else:
        reached = await path_pairs_to(repository, path, keys, fanout)
    return {near: [(far, {}) for far in sorted(fars)] for near, fars in reached.items() if fars}


async def path_affected(
    repository: MaterializedRepository, steps: Sequence[PathHop], applied: AppliedChanges
) -> set[str]:
    """Records at the start of `steps` whose pairs a changeset can change (a superset): a
    Link of step `i` that changed marks its near Record at level `i`; walking the steps
    backwards without bound gives the start Records reaching a marked Record."""
    starts: set[str] = set()
    for level, step in enumerate(steps):
        marked = await _marked(repository, step, applied)
        for back in reversed(steps[:level]):
            if not marked:
                break
            found = await links_of(
                repository, back.relation, reverse(back.direction), sorted(marked)
            )
            marked = {far for items in found.values() for far, _ in items}
        starts |= marked
    return starts


async def _marked(
    repository: MaterializedRepository, step: PathHop, applied: AppliedChanges
) -> set[str]:
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
    previous: Mapping[tuple[str, str], Mapping[str, Any] | None] = applied.previous
    for key in holders:  # targets before the change, then after it
        before = previous.get((source, key))
        if before is not None and isinstance(before.get(attribute), str):
            marked.add(before[attribute])
    if holders:
        now = await links_of(repository, relation, LinkDirection.FORWARD, sorted(holders))
        marked |= {far for items in now.values() for far, _ in items}
    return marked
