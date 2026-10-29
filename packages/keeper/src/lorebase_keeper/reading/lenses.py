"""Read presets (design 8.8): Traits as targets and lenses.

Both are resolved against the schema of the read point, which is the caller's own when
`keeper.auth.authz.invisible_kinds` is on (`reading.visibility`), and against the Kinds the
caller may read (`KindScope`) otherwise. So a preset never widens a read: a Trait stands for
the Kinds implementing it that the caller may read, and a lens as served holds only members
the caller may read.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from lorebase_core.model import Kind, Lens
from lorebase_keeper.reading.points import ReadPoint
from lorebase_keeper.reading.problems import invalid

LENS_UNKNOWN = "lens_unknown"


@dataclass(frozen=True, slots=True)
class ResolvedLens:
    """A lens and the Kinds it limits a read to: its `kinds` and the Kinds implementing its
    `traits`, readable by the caller; `None` when it names neither (no limit)."""

    lens: Lens
    kinds: tuple[Kind, ...] | None


def trait_kinds(point: ReadPoint, names: Iterable[str], pointer: str) -> list[Kind]:
    """The Kinds the caller may read that implement the Traits `names` (422 for an unknown
    Trait, which a Trait the caller cannot see is too), by Kind id."""
    schema = point.schema
    found: dict[str, Kind] = {}
    for index, name in enumerate(names):
        trait = schema.trait_by_api_name(name)
        if trait is None:
            raise invalid(f"{pointer}/{index}", f"unknown Trait {name!r}", "unknown_trait")
        for kind in schema.kinds_implementing(trait.id):
            if point.scope.allows(kind.api_name):
                found[kind.id] = kind
    return sorted(found.values(), key=lambda k: k.id)


def resolve_lens(point: ReadPoint, name: str, pointer: str) -> ResolvedLens:
    """The lens `name` for the caller; 422 `lens_unknown` when the schema has no such lens or
    all the Kinds and Traits it names are beyond the caller's grants."""
    lens = point.schema.lens_by_api_name(name)
    if lens is None:
        raise invalid(pointer, f"unknown lens {name!r}", LENS_UNKNOWN)
    if not (lens.kinds or lens.traits):
        return ResolvedLens(lens, None)
    schema = point.schema
    found: dict[str, Kind] = {}
    for kind_id in lens.kinds:
        kind = schema.kinds.get(kind_id)
        if kind is not None and point.scope.allows(kind.api_name):
            found[kind.id] = kind
    for trait_id in lens.traits:
        for kind in schema.kinds_implementing(trait_id):
            if point.scope.allows(kind.api_name):
                found[kind.id] = kind
    if not found:
        raise invalid(pointer, f"unknown lens {name!r}", LENS_UNKNOWN)
    return ResolvedLens(lens, tuple(sorted(found.values(), key=lambda k: k.id)))
