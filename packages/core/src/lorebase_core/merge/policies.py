"""Merge policies (design 6.8, 6.10): options applied before any person sees a Conflict."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lorebase_core.merge.conflicts import Side

__all__ = ["DEFAULT_ATOMIC_PROPERTIES", "MergePolicy"]

#: Schema element properties merged as whole values, never entry by entry.
DEFAULT_ATOMIC_PROPERTIES = frozenset({"type", "default", "definition"})


class MergePolicy(BaseModel):
    """Options of one merge. Every default keeps the behavior of design 6.8.

    - ``fast_forward``: when ours is an ancestor of theirs, move to theirs instead of
      producing a merge Revision (default ``True``).
    - ``allow_checked_casts``: values written on one side are cast to a type changed on the
      other side with ``checked`` casts; failures are type coercion Conflicts. When ``False``
      every such value is a type coercion Conflict (default ``True``).
    - ``cascade_dangling``: a Link added on one side to a Record deleted on the other is
      dropped when its Relation has ``on_delete = cascade`` (default ``True``).
    - ``apply_defaults``: a missing value of a newly required Attribute takes the Attribute
      default when it has one (default ``True``).
    - ``prefer``, ``prefer_elements``, ``prefer_attributes``: resolve value and schema
      Conflicts (``DV1`` to ``DV3``, ``SC1``, ``SC2``) for one side, globally, per Kind or
      Relation id, or per Attribute id (most specific wins; default: none).
    - ``sample_size``: member keys listed in grouped Conflicts (default 10).
    - ``atomic_properties``: schema element properties merged as whole values.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fast_forward: bool = True
    allow_checked_casts: bool = True
    cascade_dangling: bool = True
    apply_defaults: bool = True
    prefer: Side | None = None
    prefer_elements: dict[str, Side] = Field(default_factory=dict)
    prefer_attributes: dict[str, Side] = Field(default_factory=dict)
    sample_size: int = Field(default=10, ge=1)
    atomic_properties: frozenset[str] = DEFAULT_ATOMIC_PROPERTIES

    @field_validator("prefer")
    @classmethod
    def _side(cls, side: Side | None) -> Side | None:
        if side is Side.BASE:
            raise ValueError("prefer is ours or theirs")
        return side

    @field_validator("prefer_elements", "prefer_attributes")
    @classmethod
    def _sides(cls, sides: dict[str, Side]) -> dict[str, Side]:
        if Side.BASE in sides.values():
            raise ValueError("prefer is ours or theirs")
        return sides

    def preferred(self, element: str | None, attribute: str | None) -> Side | None:
        """The side a policy picks for a Conflict on ``element`` and ``attribute``."""
        if attribute is not None and attribute in self.prefer_attributes:
            return self.prefer_attributes[attribute]
        if element is not None and element in self.prefer_elements:
            return self.prefer_elements[element]
        return self.prefer
