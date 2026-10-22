"""Path Derivations as virtual Relations (design 5.8).

A `path` Derivation composes Relations, `A -r1-> B -r2-> C`, into a virtual Relation `A -> C`
named by the Derivation's `api_name` (forward) and `inverse_api_name` (inverse, optional).
`PathRelation` is what readers and the materializer work with: the steps resolved to
Relations and a virtual `Relation` (id = the Derivation id, explicit backing, no Attributes)
that code reading Links by Relation can use unchanged.

Semantics, shared by every evaluation (materialized rows, reads at the head, reads at a past
Revision): the Links of the path are the distinct `(a, c)` pairs such that `c` is reached from
`a` through one Link per step. The first step follows every Link of `a`; every later step
follows, from each intermediate Record, at most `fanout` Links, those with the smallest far
keys. The bound depends on the intermediate Record only, never on where the walk started, so
the pairs of one `a` can be recomputed alone. Paths have a fixed length, so a cycle in the data
ends the walk like any other step (a path may lead back to its start: `(a, a)` is a pair).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from lorebase_core.model.derivation import Derivation, PathDerivation
from lorebase_core.model.relation import Cardinality, LinkDirection, Relation, RelationBacking


@dataclass(frozen=True, slots=True)
class PathHop:
    """One step of a path: a Relation followed in a direction."""

    relation: Relation
    direction: LinkDirection

    @property
    def near_kind(self) -> str:
        return self.relation.endpoint(self.direction)

    @property
    def far_kind(self) -> str:
        return self.relation.far_endpoint(self.direction)


@dataclass(frozen=True, slots=True)
class PathRelation:
    """A valid path Derivation with its steps resolved (see the module docstring)."""

    derivation: Derivation
    relation: Relation
    steps: tuple[PathHop, ...]

    @property
    def id(self) -> str:
        return self.derivation.id

    @property
    def materialized(self) -> bool:
        definition = self.derivation.definition
        assert isinstance(definition, PathDerivation)
        return definition.materialized

    @property
    def kinds(self) -> tuple[str, ...]:
        """Kind ids along the path, from the first `near` Kind to the last `far` Kind."""
        return (self.steps[0].near_kind, *(step.far_kind for step in self.steps))

    @property
    def inner_kinds(self) -> tuple[str, ...]:
        """Kind ids of the intermediate Records."""
        return self.kinds[1:-1]


def path_relation(derivation: Derivation, relations: Mapping[str, Relation]) -> PathRelation | None:
    """The resolved path of a `path` Derivation; `None` for other variants and for paths
    naming unknown Relations or not continuing from one step to the next (schema validation
    reports those)."""
    definition = derivation.definition
    if not isinstance(definition, PathDerivation):
        return None
    steps: list[PathHop] = []
    for step in definition.steps:
        relation = relations.get(step.relation)
        if relation is None:
            return None
        hop = PathHop(relation, step.direction)
        if steps and steps[-1].far_kind != hop.near_kind:
            return None
        steps.append(hop)
    virtual = Relation.model_construct(
        id=derivation.id,
        api_name=derivation.api_name,
        display_name=derivation.display_name,
        description=derivation.description,
        inverse_api_name=definition.inverse_api_name or "",
        from_kind=steps[0].near_kind,
        to_kind=steps[-1].far_kind,
        cardinality=Cardinality.MANY_TO_MANY,
        attributes={},
        backing=RelationBacking.EXPLICIT,
        status=derivation.status,
    )
    return PathRelation(derivation, virtual, tuple(steps))


def path_relations(
    derivations: Iterable[Derivation], relations: Mapping[str, Relation]
) -> dict[str, PathRelation]:
    """The valid path Derivations by Derivation id."""
    found: dict[str, PathRelation] = {}
    for derivation in derivations:
        path = path_relation(derivation, relations)
        if path is not None:
            found[derivation.id] = path
    return found


def bounded(far_keys: Iterable[str], fanout: int | None) -> list[str]:
    """The far keys one intermediate Record contributes: distinct, smallest first, at most
    `fanout` (all when `None`)."""
    ordered = sorted(set(far_keys))
    return ordered if fanout is None else ordered[:fanout]


def walk(
    starts: Iterable[str],
    steps: Sequence[Callable[[Sequence[str]], Mapping[str, Sequence[str]]]],
    fanout: int | None,
) -> dict[str, set[str]]:
    """Pairs of a path from `starts`, with each step given as a function from near keys to
    their far keys (synchronous loaders: tree reads hold every Link in memory)."""
    reached: dict[str, set[str]] = {start: {start} for start in starts}
    for index, step in enumerate(steps):
        frontier = sorted({key for keys in reached.values() for key in keys})
        found = step(frontier) if frontier else {}
        nexts = {
            near: (list(far) if index == 0 else bounded(far, fanout)) for near, far in found.items()
        }
        reached = {
            start: {far for near in keys for far in nexts.get(near, ())}
            for start, keys in reached.items()
        }
    return reached
