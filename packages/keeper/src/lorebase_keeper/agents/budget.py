"""Token budget of context packs: a cheap deterministic estimate and the trimming order.

A pack is estimated section by section: `tokens = ceil(chars / chars_per_token)` over the
exact text each section renders to in the requested format (compact JSON or markdown). No
tokenizer and no model are involved, so the same request at the same Revision always yields
the same pack.

When the sections exceed the budget, trim steps are applied in the configured order
(`agents.context.trim_order`; default `DEFAULT_TRIM_ORDER`) until the pack fits:

1. `lineage`: lineage entries, last first;
2. `recent_changes`: change entries, oldest first;
3. `far_neighbor_values`: values of neighbors beyond depth 1 (their reference and title stay);
4. `far_neighbors`: neighbors beyond depth 1 with their Links, deepest and last first;
5. `schema_descriptions`: descriptions in the schema fragment (names and types stay);
6. `near_neighbor_values`: values of depth 1 neighbors;
7. `near_neighbors`: depth 1 neighbors with their Links, last first;
8. `schema`: the schema fragment.

Each step removes as few units as needed (found by bisection: removing units never grows a
section). Seed Records are never trimmed; a pack whose seeds alone exceed the budget is
returned whole and flagged `over_budget`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any


class TrimStep(StrEnum):
    LINEAGE = "lineage"
    RECENT_CHANGES = "recent_changes"
    FAR_NEIGHBOR_VALUES = "far_neighbor_values"
    FAR_NEIGHBORS = "far_neighbors"
    SCHEMA_DESCRIPTIONS = "schema_descriptions"
    NEAR_NEIGHBOR_VALUES = "near_neighbor_values"
    NEAR_NEIGHBORS = "near_neighbors"
    SCHEMA = "schema"


DEFAULT_TRIM_ORDER: tuple[TrimStep, ...] = tuple(TrimStep)
NEAR_DEPTH = 1
"""Neighbors at this depth or closer are `near`; deeper ones are `far`."""


def estimate_tokens(chars: int, chars_per_token: float) -> int:
    """Estimated tokens of `chars` characters."""
    return math.ceil(chars / chars_per_token)


@dataclass(frozen=True, slots=True)
class SeedItem:
    ref: str
    kind: str
    title: str | None
    values: Mapping[str, Any]
    updated_revision: str | None = None


@dataclass(frozen=True, slots=True)
class NeighborItem:
    ref: str
    kind: str
    title: str | None
    depth: int
    values: Mapping[str, Any] | None
    """`None` once trimmed: only the reference and title remain."""


@dataclass(frozen=True, slots=True)
class LinkItem:
    relation: str
    """Forward api name of the Relation."""
    source: str
    target: str
    values: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChangeItem:
    revision: str
    at: str
    ref: str
    change: str
    attributes: tuple[str, ...]
    actor: str
    origin: str
    message: str


@dataclass(frozen=True, slots=True)
class LineageItem:
    ref: str
    origin: str | None
    actor: str | None
    at: str | None
    attributes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Draft:
    """The content of a pack, in output order. `None` marks a section left out."""

    seeds: tuple[SeedItem, ...]
    neighbors: tuple[NeighborItem, ...] = ()
    links: tuple[LinkItem, ...] = ()
    schema: bool = False
    schema_descriptions: bool = True
    changes: tuple[ChangeItem, ...] | None = None
    lineage: tuple[LineageItem, ...] | None = None


@dataclass(frozen=True, slots=True)
class Trim:
    step: TrimStep
    removed: int


@dataclass(frozen=True, slots=True)
class BudgetResult:
    draft: Draft
    trims: tuple[Trim, ...]
    sections: Mapping[str, int]
    """Characters per section of the final draft."""
    limit_chars: float

    @property
    def chars(self) -> int:
        return sum(self.sections.values())

    @property
    def over_budget(self) -> bool:
        return self.chars > self.limit_chars


Measure = Callable[[Draft], Mapping[str, int]]
"""Characters of each section of a draft, as rendered in the requested format."""


def _is_near(item: NeighborItem) -> bool:
    return item.depth <= NEAR_DEPTH


def _candidates(draft: Draft, near: bool, *, with_values: bool) -> list[int]:
    """Neighbor positions in removal order (deepest, then last in output order, first)."""
    positions = [
        i
        for i, item in enumerate(draft.neighbors)
        if _is_near(item) is near and (not with_values or item.values is not None)
    ]
    return sorted(positions, key=lambda i: (-draft.neighbors[i].depth, -i))


def units(draft: Draft, step: TrimStep) -> int:
    """How many units `step` can remove from `draft`."""
    match step:
        case TrimStep.LINEAGE:
            return len(draft.lineage or ())
        case TrimStep.RECENT_CHANGES:
            return len(draft.changes or ())
        case TrimStep.FAR_NEIGHBOR_VALUES | TrimStep.NEAR_NEIGHBOR_VALUES:
            near = step is TrimStep.NEAR_NEIGHBOR_VALUES
            return len(_candidates(draft, near, with_values=True))
        case TrimStep.FAR_NEIGHBORS | TrimStep.NEAR_NEIGHBORS:
            return len(_candidates(draft, step is TrimStep.NEAR_NEIGHBORS, with_values=False))
        case TrimStep.SCHEMA_DESCRIPTIONS:
            return 1 if draft.schema and draft.schema_descriptions else 0
        case TrimStep.SCHEMA:
            return 1 if draft.schema else 0


def apply(draft: Draft, step: TrimStep, count: int) -> Draft:
    """`draft` with the first `count` units of `step` removed."""
    if count <= 0:
        return draft
    match step:
        case TrimStep.LINEAGE:
            lineage = draft.lineage or ()
            return replace(draft, lineage=lineage[: max(len(lineage) - count, 0)])
        case TrimStep.RECENT_CHANGES:
            return replace(draft, changes=(draft.changes or ())[count:])
        case TrimStep.FAR_NEIGHBOR_VALUES | TrimStep.NEAR_NEIGHBOR_VALUES:
            near = step is TrimStep.NEAR_NEIGHBOR_VALUES
            stripped = set(_candidates(draft, near, with_values=True)[:count])
            neighbors = tuple(
                replace(item, values=None) if i in stripped else item
                for i, item in enumerate(draft.neighbors)
            )
            return replace(draft, neighbors=neighbors)
        case TrimStep.FAR_NEIGHBORS | TrimStep.NEAR_NEIGHBORS:
            near = step is TrimStep.NEAR_NEIGHBORS
            dropped = set(_candidates(draft, near, with_values=False)[:count])
            gone = {draft.neighbors[i].ref for i in dropped}
            return replace(
                draft,
                neighbors=tuple(n for i, n in enumerate(draft.neighbors) if i not in dropped),
                links=tuple(
                    link
                    for link in draft.links
                    if link.source not in gone and link.target not in gone
                ),
            )
        case TrimStep.SCHEMA_DESCRIPTIONS:
            return replace(draft, schema_descriptions=False)
        case TrimStep.SCHEMA:
            return replace(draft, schema=False)


def fit(
    draft: Draft,
    *,
    budget_tokens: int,
    chars_per_token: float,
    measure: Measure,
    order: Sequence[TrimStep] = DEFAULT_TRIM_ORDER,
) -> BudgetResult:
    """Trim `draft` by `order` until its sections fit `budget_tokens` (see module doc)."""
    limit = budget_tokens * chars_per_token

    def size(candidate: Draft) -> int:
        return sum(measure(candidate).values())

    current = draft
    trims: list[Trim] = []
    for step in order:
        if size(current) <= limit:
            break
        available = units(current, step)
        if available == 0:
            continue
        count = available
        if size(apply(current, step, available)) <= limit:
            low, high = 1, available
            while low < high:
                middle = (low + high) // 2
                if size(apply(current, step, middle)) <= limit:
                    high = middle
                else:
                    low = middle + 1
            count = low
        current = apply(current, step, count)
        trims.append(Trim(step, count))
    return BudgetResult(current, tuple(trims), dict(measure(current)), limit)
