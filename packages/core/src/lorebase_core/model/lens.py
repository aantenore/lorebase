"""Lens (design 5.13, 8.8): a named, schema-validated preset of a read.

A lens says which Kinds and Traits a read is about, which Relations to follow, how deep, which
optional sections to include and a budget. It is a preset, not a permission: grants apply to
everything it names. References are ids like every other reference, so renames do not break it.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from lorebase_core.model.base import SchemaElement
from lorebase_core.model.ids import KindId, LensId, RelationId, TraitId


class Lens(SchemaElement):
    object_type: ClassVar[str] = "schema.lens"

    id: LensId
    #: Kinds the read is about; they and the Kinds implementing ``traits`` limit neighbors and
    #: the Kinds of search and query seeds. Both empty: no limit.
    kinds: tuple[KindId, ...] = Field(default_factory=tuple)
    traits: tuple[TraitId, ...] = Field(default_factory=tuple)
    relations: tuple[RelationId, ...] = Field(default_factory=tuple)
    #: Not range checked here: static validation reports a depth below 0 (8.8).
    depth: int | None = None
    include_schema: bool | None = None
    include_recent_changes: bool | None = None
    include_lineage: bool | None = None
    budget_tokens: int | None = None
