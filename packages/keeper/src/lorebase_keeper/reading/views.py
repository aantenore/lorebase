"""Response shapes of the read API and the mapping from stored rows to them.

Responses use api names (design 8.1); values stay in canonical form (design 6.1): integers and
decimals are JSON strings, timestamps RFC 3339 UTC with six fractional digits. Values of
Attributes the schema no longer knows are left out; values of sensitive Attributes are masked
for callers without the clearance (`reading.sensitivity`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, JsonValue

from lorebase_core.model import LinkDirection, Relation, Schema
from lorebase_core.query import FieldRef, FieldSource
from lorebase_keeper.reading.points import ReadPoint, ServedFrom
from lorebase_keeper.reading.sensitivity import Masking


class Served(BaseModel):
    """Where a response was read: Revision id and materialized head or tree."""

    revision: str = Field(description="Revision the response was served from (hex).")
    served_from: ServedFrom


class LinkCountOut(BaseModel):
    relation: str = Field(description="Relation api name in the direction seen from the Record.")
    direction: LinkDirection
    count: int


class LineageOut(BaseModel):
    provenance_id: str
    origin_type: str | None = None
    actor: str | None = None
    at: datetime | None = None


class RecordOut(BaseModel):
    """A Record with values keyed by Attribute api name."""

    kind: str
    key: str
    title: str | None = None
    values: dict[str, JsonValue]
    updated_revision: str | None = None
    updated_at: datetime | None = None
    links: list[LinkCountOut] | None = None
    lineage: dict[str, LineageOut] | None = None
    included: dict[str, list[RecordOut]] | None = None


class RecordResponse(Served):
    record: RecordOut


class RecordPage(Served):
    items: list[RecordOut]
    next_cursor: str | None = None


class AggregateCount(BaseModel):
    kind: str
    value: JsonValue = Field(default=None, description="Group value when `group_by` is given.")
    count: int


class AggregateOut(BaseModel):
    counts: list[AggregateCount]
    truncated: bool = False


class QueryResponse(RecordPage):
    aggregate: AggregateOut | None = None


class LinkOut(BaseModel):
    """A Link seen from one side: `relation` is the api name in that direction."""

    relation: str
    direction: LinkDirection
    from_key: str
    to_key: str
    values: dict[str, JsonValue]
    target: RecordRef | None = Field(
        default=None, description="The Record at the other end, as seen from the source."
    )


class RecordRef(BaseModel):
    kind: str
    key: str
    title: str | None = None


class LinkPage(Served):
    items: list[LinkOut]
    next_cursor: str | None = None


class Edge(BaseModel):
    relation: str
    source: RecordRef = Field(alias="from")
    to: RecordRef
    values: dict[str, JsonValue]

    model_config = {"populate_by_name": True, "serialize_by_alias": True}


class EdgeRef(BaseModel):
    """A Link by its endpoints only (`values: false`)."""

    relation: str
    source: RecordRef = Field(alias="from")
    to: RecordRef

    model_config = {"populate_by_name": True, "serialize_by_alias": True}


class TraverseHopOut(BaseModel):
    relation: str
    records: list[RecordOut]
    edges: list[Edge]
    truncated: bool = False


class TraverseResponse(Served):
    start: RecordOut
    hops: list[TraverseHopOut]


class NeighborhoodResponse(Served):
    nodes: list[RecordOut | RecordRef] = Field(
        description="Records reached, nearest first: headers only (`RecordRef`) when the "
        "request has `values: false`."
    )
    edges: list[Edge | EdgeRef] = Field(
        description="Links between the nodes: endpoints only (`EdgeRef`) when the request has "
        "`values: false`."
    )
    truncated: bool = False


class SearchHit(BaseModel):
    kind: str
    key: str
    title: str | None = None
    snippet: str | None = None
    score: float
    match: Literal["all", "partial"] | None = Field(
        default=None,
        description="How the hit matched q: every term (`all`), or only some of them "
        "(`partial`, ranked after every `all` hit); absent for hits found by vector only.",
    )


class SearchCorrection(BaseModel):
    """A query word the branch does not hold and the words searched in its place as well.

    Words are lexemes of the text search configuration (lowercased, stemmed by its
    dictionaries), as full-text search compares them.
    """

    word: str
    replacements: list[str]


class SearchResponse(Served):
    mode: str
    items: list[SearchHit]
    corrections: list[SearchCorrection] = Field(
        default_factory=list,
        description="Typo corrections applied to q; hits found only through them come after "
        "the others.",
    )
    truncated_candidates: bool = Field(
        default=False,
        description="Only a sample of the matches of q (the first ones in index order) was "
        "ranked, as happens when every term of q is very common: better matches may exist. "
        "Add a rarer term, a Kind or a filter to narrow q.",
    )
    semantic_truncated: bool = Field(
        default=False,
        description="The vector list is shorter than asked for because the index scan reached "
        "its bound (search.semantic.max_scan_tuples, or the over-fetched nearest rows) while "
        "more Records match the filters: narrow the Kinds or the filter, or raise the bound.",
    )


# Rendering ----------------------------------------------------------------------------------


def kind_name(schema: Schema, kind_id: str) -> str:
    kind = schema.kinds.get(kind_id)
    return kind.api_name if kind is not None else kind_id


def relation_name(relation: Relation, direction: LinkDirection) -> str:
    return relation.api_name if direction is LinkDirection.FORWARD else relation.inverse_api_name


def record_out(
    schema: Schema,
    row: Mapping[Any, Any],
    *,
    masking: Masking,
    select: Sequence[FieldRef] | None = None,
) -> RecordOut:
    """API form of a materialized row (`record_columns`) or a tree Record, with the values the
    caller is not cleared for masked."""
    kind_id = row["kind_id"]
    kind = schema.kinds.get(kind_id)
    values: Mapping[str, Any] = row.get("values") or {}
    derived: Mapping[str, Any] = row.get("derived") or {}
    rendered = {} if kind is None else masking.kind_values(kind, values, derived=derived)
    if select is not None:
        wanted = _selected(select, kind_id, kind.attributes if kind is not None else {})
        rendered = {name: value for name, value in rendered.items() if name in wanted}
    updated = row.get("updated_revision")
    return RecordOut(
        kind=kind.api_name if kind is not None else kind_id,
        key=row["key"],
        title=masking.title(schema, kind_id, row.get("title")),
        values=rendered,
        updated_revision=None if updated is None else bytes(updated).hex(),
        updated_at=row.get("updated_at"),
    )


def _selected(select: Sequence[FieldRef], kind_id: str, attributes: Mapping[str, Any]) -> set[str]:
    wanted: set[str] = set()
    for ref in select:
        if ref.source in (FieldSource.KEY, FieldSource.TITLE):
            continue
        attribute_id = ref.attributes.get(kind_id)
        attribute = attributes.get(attribute_id) if attribute_id is not None else None
        if attribute is not None:
            wanted.add(attribute.api_name)
    return wanted


def record_ref(
    schema: Schema,
    kind_id: str,
    key: str,
    title: str | None = None,
    masking: Masking | None = None,
) -> RecordRef:
    """A Record reference; a title needs `masking` to be shown."""
    shown = None if masking is None else masking.title(schema, kind_id, title)
    return RecordRef(kind=kind_name(schema, kind_id), key=key, title=shown)


def link_values(
    schema: Schema, relation: Relation, values: Mapping[str, Any] | None, masking: Masking
) -> dict[str, Any]:
    """Link Attribute values keyed by api name, masked for the caller."""
    return masking.link_values(schema, relation, values)


def served(point: ReadPoint) -> dict[str, Any]:
    return {"revision": point.revision_hex, "served_from": point.served_from}


RecordOut.model_rebuild()
LinkOut.model_rebuild()
