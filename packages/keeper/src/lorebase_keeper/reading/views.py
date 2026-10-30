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


class AttributeCoverage(BaseModel):
    """How many Records of a Kind miss an Attribute the request used."""

    kind: str
    attribute: str
    missing: float = Field(description="Share of the Kind's Records without a value, 0 to 1.")
    missing_records: int = Field(description="Records without a value (estimated if sampled).")
    used_in: list[Literal["where", "select", "order_by", "resolve"]] = Field(
        description="Where the request used the Attribute (resolve: a candidate named it)."
    )


class SourceFreshness(BaseModel):
    """When a Source feeding a Kind last synchronized, and what it has not seen lately."""

    kind: str
    source: str = Field(description="Source api name.")
    last_synced_at: datetime | None = Field(
        description="End of its last successful run (dry runs aside); null: never."
    )
    stale_after: str | None = Field(default=None, description="The Source's stale_after.")
    stale: bool = Field(
        description="Its last successful run is older than stale_after (or it never ran)."
    )
    unseen_records: int | None = Field(
        default=None,
        description="Records it owns that no run has seen for stale_after (incremental "
        "Sources with stale_after), as of the statistics.",
    )


class KindCoverage(BaseModel):
    """The statistics the coverage of a Kind was read from."""

    kind: str
    records: int | None = Field(description="Live Records of the Kind when counted.")
    as_of: str | None = Field(description="Revision (hex) the statistics were counted at.")
    computed_at: datetime | None = None
    approximate: bool = Field(
        description="The head moved since, or the Kind was counted over a sample."
    )


class CoverageOut(BaseModel):
    """How complete an answer can be (design 8.11): an empty result is not missing data. Read
    from statistics of the default branch head, never from the Records; only Kinds and
    Attributes the caller may read are described."""

    kinds: list[KindCoverage] = Field(default_factory=list)
    attributes: list[AttributeCoverage] = Field(default_factory=list)
    sources: list[SourceFreshness] = Field(default_factory=list)
    unknown: list[str] = Field(
        default_factory=list,
        description="Resolution: phrases that matched nothing in this Domain (no Kind, "
        "Attribute or indexed value).",
    )
    notes: list[str] = Field(
        default_factory=list, description="One line per signal, for agents and people."
    )
    omitted: str | None = Field(
        default=None, description="Why statistics are left out (e.g. not the default branch)."
    )


class QueryResponse(RecordPage):
    aggregate: AggregateOut | None = None
    coverage: CoverageOut | None = Field(
        default=None, description="How complete the answer can be (unless coverage: false)."
    )


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


class ResolveCandidate(BaseModel):
    """A schema element or indexed value some words of the text may stand for."""

    target: Literal["kind", "attribute", "value"] = Field(
        description="What matched: a Kind, an Attribute (by name), or values of an Attribute."
    )
    kind: str = Field(description="Kind api name.")
    attribute: str | None = Field(
        default=None, description="Attribute api name (attribute and value candidates)."
    )
    type: str | None = Field(default=None, description="Attribute type, e.g. decimal(12,2).")
    values: list[str] | None = Field(
        default=None, description="Stored values the words stand for (value candidates)."
    )
    predicate: dict[str, Any] | None = Field(
        default=None,
        description="Ready-to-use where fragment of lorebase_query / POST /query for the Kind "
        "(value candidates).",
    )
    score: float = Field(description="Confidence, 0 to 1; higher first.")
    level: Literal["exact", "synonym", "trigram", "embedding"] = Field(
        description="How it matched: exact name or value, alias, similar spelling, or values "
        "of the Records nearest to the query vector."
    )
    matched: str = Field(description="The words of the text that matched.")
    records: int | None = Field(
        default=None,
        description="Records holding the values on the branch read (value candidates).",
    )


class ResolveSkipped(BaseModel):
    level: str
    reason: str


class ResolveResponse(Served):
    candidates: list[ResolveCandidate]
    unmatched: list[str] = Field(
        default_factory=list, description="Words no candidate covers (often filler words)."
    )
    skipped: list[ResolveSkipped] = Field(
        default_factory=list, description="Levels that did not run, and why."
    )
    coverage: CoverageOut | None = Field(
        default=None,
        description="Phrases unknown in this Domain and how many Records miss the Attributes "
        "of the candidates (unless coverage: false).",
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
