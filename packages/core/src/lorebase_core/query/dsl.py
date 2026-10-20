"""The JSON query DSL (design 8.2): request models only, no schema knowledge.

A query names a Kind (or a Trait, which expands to the Kinds implementing it), an optional
`where` predicate, the fields to return, Links to include, the ordering and a page size:

    {
      "kind": "Part",
      "where": {"and": [
        {"attr": "category", "op": "eq", "value": "bearing"},
        {"via": "supplied_by", "where": {"attr": "country", "op": "in", "value": ["IT", "DE"]}},
        {"count": "supplied_by", "op": "gte", "value": 2}
      ]},
      "select": ["key", "title", "category"],
      "include": {"supplied_by": {"select": ["key", "title"], "limit": 5}},
      "order_by": [{"attr": "category"}, {"attr": "key", "dir": "desc"}],
      "limit": 50,
      "aggregate": {"group_by": "category"}
    }

Predicates are JSON objects told apart by their leading member:

- `{"and": [...]}`, `{"or": [...]}`, `{"not": {...}}`: boolean composition;
- `{"attr": name, "op": op, "value": v}`: compare an Attribute (or the pseudo fields `key` and
  `title`); `{"derived": name, ...}` does the same for a derived Attribute;
- `{"via": relation, "where": {...}}`: a Link of the Relation (forward or inverse api name)
  exists, optionally to a Record matching `where`;
- `{"count": relation, "op": op, "value": n, "where": {...}}`: compare the number of such Links;
- `{"match": "text"}`: full-text match on the Record's search vector.

Validation against a Schema lives in `lorebase_core.query.resolve`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    JsonValue,
    Tag,
    model_validator,
)

__all__ = [
    "Aggregate",
    "AllOf",
    "AnyOf",
    "AttributeCondition",
    "CountCondition",
    "DerivedCondition",
    "Include",
    "MatchCondition",
    "NotCondition",
    "Operator",
    "OrderBy",
    "Predicate",
    "Query",
    "ViaCondition",
]


class QueryModel(BaseModel):
    """Frozen request model; unknown members are rejected."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class Operator(StrEnum):
    """Comparison operators; which ones apply depends on the Attribute type."""

    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    IN = "in"
    BETWEEN = "between"
    CONTAINS = "contains"
    PREFIX = "prefix"
    EXISTS = "exists"
    MATCH = "match"
    NEAR = "near"


class AttributeCondition(QueryModel):
    """Compare an Attribute, `key` or `title` with a value."""

    attr: str = Field(min_length=1)
    op: Operator
    value: JsonValue = None


class DerivedCondition(QueryModel):
    """Compare a derived Attribute with a value."""

    derived: str = Field(min_length=1)
    op: Operator
    value: JsonValue = None


class ViaCondition(QueryModel):
    """A Link of `via` exists, to a Record matching `where` when given."""

    via: str = Field(min_length=1)
    where: Predicate | None = None


class CountCondition(QueryModel):
    """Compare the number of Links of `count` (to Records matching `where`) with a value."""

    count: str = Field(min_length=1)
    op: Operator
    value: JsonValue
    where: Predicate | None = None


class MatchCondition(QueryModel):
    """Full-text match of the Record (title and searchable Attributes)."""

    match: str = Field(min_length=1)


class AllOf(QueryModel):
    items: list[Predicate] = Field(alias="and", min_length=1)


class AnyOf(QueryModel):
    items: list[Predicate] = Field(alias="or", min_length=1)


class NotCondition(QueryModel):
    item: Predicate = Field(alias="not")


_TAGS = ("and", "or", "not", "via", "count", "match", "derived", "attr")
_TAG_OF_MODEL: dict[type[QueryModel], str] = {}


def _predicate_tag(value: Any) -> str | None:
    """Discriminate predicates by their leading member (raw JSON) or by model class."""
    if isinstance(value, dict):
        for tag in _TAGS:
            if tag in value:
                return tag
        return None
    return _TAG_OF_MODEL.get(type(value))


Predicate = Annotated[
    Annotated[AllOf, Tag("and")]
    | Annotated[AnyOf, Tag("or")]
    | Annotated[NotCondition, Tag("not")]
    | Annotated[ViaCondition, Tag("via")]
    | Annotated[CountCondition, Tag("count")]
    | Annotated[MatchCondition, Tag("match")]
    | Annotated[DerivedCondition, Tag("derived")]
    | Annotated[AttributeCondition, Tag("attr")],
    Discriminator(
        _predicate_tag,
        custom_error_type="predicate",
        custom_error_message="a predicate needs one of the members " + ", ".join(_TAGS),
    ),
]
"""Any predicate of the DSL."""

_TAG_OF_MODEL.update(
    {
        AllOf: "and",
        AnyOf: "or",
        NotCondition: "not",
        ViaCondition: "via",
        CountCondition: "count",
        MatchCondition: "match",
        DerivedCondition: "derived",
        AttributeCondition: "attr",
    }
)


class OrderBy(QueryModel):
    """One ordering term: an Attribute, `key` or `title`. Absent values sort last."""

    attr: str = Field(min_length=1)
    dir: Literal["asc", "desc"] = "asc"


class Include(QueryModel):
    """Neighbors to return with each Record, through one Relation."""

    select: list[str] | None = None
    limit: int | None = Field(default=None, ge=1)
    where: Predicate | None = None


class Aggregate(QueryModel):
    """Count the matching Records per Kind, and per value of `group_by` when given."""

    group_by: str | None = None


class Query(QueryModel):
    """A query over the Records of one Kind or of the Kinds implementing a Trait."""

    kind: str | None = None
    trait: str | None = None
    where: Predicate | None = None
    select: list[str] | None = None
    include: dict[str, Include] = Field(default_factory=dict)
    order_by: list[OrderBy] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=0)
    cursor: str | None = None
    aggregate: Aggregate | None = None

    @model_validator(mode="after")
    def _one_target(self) -> Self:
        if (self.kind is None) == (self.trait is None):
            raise ValueError("a query names exactly one of kind or trait")
        return self


for _model in (ViaCondition, CountCondition, AllOf, AnyOf, NotCondition, Include, Query):
    _model.model_rebuild()
