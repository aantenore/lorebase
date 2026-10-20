"""Response shapes of the read API and the mapping from stored rows to them.

Responses use api names (design 8.1); values stay in canonical form (design 6.1): integers and
decimals are JSON strings, timestamps RFC 3339 UTC with six fractional digits. Values of
Attributes the schema no longer knows are left out.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, JsonValue

from lorebase_core.model import LinkDirection, Relation, Schema
from lorebase_core.query import FieldRef, FieldSource
from lorebase_keeper.reading.points import ReadPoint, ServedFrom


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


class TraverseHopOut(BaseModel):
    relation: str
    records: list[RecordOut]
    edges: list[Edge]
    truncated: bool = False


class TraverseResponse(Served):
    start: RecordOut
    hops: list[TraverseHopOut]


class NeighborhoodResponse(Served):
    nodes: list[RecordOut]
    edges: list[Edge]
    truncated: bool = False


class SearchHit(BaseModel):
    kind: str
    key: str
    title: str | None = None
    snippet: str | None = None
    score: float


class SearchResponse(Served):
    mode: str
    items: list[SearchHit]


# Rendering ----------------------------------------------------------------------------------


def kind_name(schema: Schema, kind_id: str) -> str:
    kind = schema.kinds.get(kind_id)
    return kind.api_name if kind is not None else kind_id


def relation_name(relation: Relation, direction: LinkDirection) -> str:
    return relation.api_name if direction is LinkDirection.FORWARD else relation.inverse_api_name


def named_values(
    attributes: Mapping[str, Any],
    values: Mapping[str, Any],
    derived: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Values keyed by api name, in schema order; ids unknown to `attributes` are dropped."""
    out: dict[str, Any] = {}
    for attribute_id, attribute in attributes.items():
        source = derived if attribute.is_derived and derived is not None else values
        if attribute_id in source:
            out[attribute.api_name] = source[attribute_id]
    return out


def record_out(
    schema: Schema,
    row: Mapping[Any, Any],
    *,
    select: Sequence[FieldRef] | None = None,
) -> RecordOut:
    """API form of a materialized row (`record_columns`) or a tree Record."""
    kind_id = row["kind_id"]
    kind = schema.kinds.get(kind_id)
    values: Mapping[str, Any] = row.get("values") or {}
    derived: Mapping[str, Any] = row.get("derived") or {}
    rendered = {} if kind is None else named_values(kind.attributes, values, derived)
    if select is not None:
        wanted = _selected(select, kind_id, kind.attributes if kind is not None else {})
        rendered = {name: value for name, value in rendered.items() if name in wanted}
    updated = row.get("updated_revision")
    return RecordOut(
        kind=kind.api_name if kind is not None else kind_id,
        key=row["key"],
        title=row.get("title"),
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


def record_ref(schema: Schema, kind_id: str, key: str, title: str | None = None) -> RecordRef:
    return RecordRef(kind=kind_name(schema, kind_id), key=key, title=title)


def link_values(relation: Relation, values: Mapping[str, Any] | None) -> dict[str, Any]:
    return named_values(relation.attributes, values or {})


def served(point: ReadPoint) -> dict[str, Any]:
    return {"revision": point.revision_hex, "served_from": point.served_from}


RecordOut.model_rebuild()
LinkOut.model_rebuild()
