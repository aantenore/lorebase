"""`GET .../records/{key}/explain`: why a Record exists and why its values are what they are.

Read at the materialized head of a branch, from the branch history (`mat_history`, the
first-parent changesets), the provenance entries (design 5.11) and the Revisions:

- `created`: the Revision that last created the Record (its first change, or the change after
  a deletion), its author, message and origin, and the provenance of that change;
- `attributes`: per Attribute, the provenance of its last change (`mat_records.lineage`) and
  the Revision that made it;
- `links`: the Links of the Record and when each was made (Links backed by a foreign key are
  attributed to the change of that key Attribute);
- `history`: how many changes the Record has on the branch;
- `proposals`: merged Proposals whose merge Revision changed the Record, or whose conflict
  resolutions set one of its values.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy import ColumnElement, Table, and_, func, literal, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Kind, LinkDirection
from lorebase_core.model.relation import RelationBacking
from lorebase_core.query import Hop, Target
from lorebase_core.tree import kind_partition, link_key, relation_partition
from lorebase_keeper.agents.context import ref_of, timestamp
from lorebase_keeper.agents.markdown import pairs, short, table, text
from lorebase_keeper.agents.pack import ordered_attributes, ordered_values
from lorebase_keeper.agents.settings import ExplainSettings
from lorebase_keeper.reading.history import REVISIONS
from lorebase_keeper.reading.points import ReadPoint
from lorebase_keeper.reading.records import compiler_for, fetch_record
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.reading.snapshots import with_rows
from lorebase_keeper.reading.views import record_out, relation_name
from lorebase_store.materialize.rows import ELEMENT_PROVENANCE_KEY
from lorebase_store.models import metadata
from lorebase_store.repositories import MaterializedRepository, ProvenanceRepository
from lorebase_store.repositories.provenance import ProvenanceRow

PROPOSALS: Table = metadata.tables["proposals"]
MERGE_RESOLUTION = "merge_resolution"


# Response shape -------------------------------------------------------------------------------


class _Out(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


class OriginOut(_Out):
    """Where a change came from: the Revision and the provenance entry."""

    revision: str | None = None
    at: str | None = None
    actor: str | None = None
    origin_type: str | None = Field(default=None, description="Provenance origin type.")
    origin: dict[str, JsonValue] | None = Field(default=None, description="Provenance origin.")
    message: str | None = Field(default=None, description="Message of the Revision.")


class CreatedOut(OriginOut):
    revision_origin: dict[str, JsonValue] | None = Field(
        default=None, description="Origin of the Revision (`type`, `ref`)."
    )


class AttributeLineageOut(OriginOut):
    name: str
    value: JsonValue = None


class LinkLineageOut(OriginOut):
    relation: str = Field(description="Relation api name seen from the Record.")
    direction: LinkDirection
    ref: str = Field(description="The Record at the other end.")
    title: str | None = None
    values: dict[str, JsonValue] | None = None


class ExplainedRecordOut(_Out):
    ref: str
    kind: str
    key: str
    title: str | None = None
    values: dict[str, JsonValue]
    updated_revision: str | None = None
    updated_at: str | None = None


class HistoryOut(_Out):
    changes: int
    first_revision: str | None = None
    last_revision: str | None = None


class ProposalOut(_Out):
    id: str
    title: str
    status: str
    merged_revision: str | None = None


class ExplainOut(_Out):
    """An explained Record (`format: json`)."""

    domain: str
    branch: str
    revision: str
    record: ExplainedRecordOut
    created: CreatedOut | None = None
    attributes: list[AttributeLineageOut]
    links: list[LinkLineageOut]
    links_truncated: bool
    history: HistoryOut
    proposals: list[ProposalOut]


# Building -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Revision:
    id: str
    at: str | None
    author: str
    message: str
    origin: dict[str, Any]


def _compact(values: Mapping[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in values.items() if value is not None}


def _origin(
    entry: ProvenanceRow | None, revision: _Revision | None, at: str | None = None
) -> dict[str, Any]:
    return _compact(
        {
            "revision": None if revision is None else revision.id,
            "at": timestamp(entry.at) if entry is not None else at,
            "actor": entry.actor if entry is not None else (revision and revision.author),
            "origin_type": None if entry is None else entry.origin_type,
            "origin": None if entry is None else dict(entry.origin_ref),
            "message": None if revision is None else revision.message,
        }
    )


async def _revisions(
    session: AsyncSession, domain_id: str, ids: Iterable[bytes]
) -> dict[bytes, _Revision]:
    wanted = list(dict.fromkeys(ids))
    if not wanted:
        return {}
    r = REVISIONS
    result = await session.execute(
        select(r.c.id, r.c.created_at, r.c.author, r.c.message, r.c.origin).where(
            r.c.domain_id == domain_id, r.c.id.in_(wanted)
        )
    )
    return {
        bytes(row.id): _Revision(
            bytes(row.id).hex(), timestamp(row.created_at), row.author, row.message, row.origin
        )
        for row in result
    }


def _hops(point: ReadPoint, kind: Kind) -> list[Hop]:
    """Relations touching the Kind, both directions, whose far Kind the caller may read."""
    hops: list[Hop] = []
    for relation in point.schema.relations_of(kind.id):
        for direction in LinkDirection:
            if relation.endpoint(direction) != kind.id:
                continue
            far = point.schema.kinds.get(relation.far_endpoint(direction))
            if far is None or not point.scope.allows(far.api_name):
                continue
            name = relation_name(relation, direction)
            hops.append(Hop(name, relation, direction, kind.id, Target(far.api_name, (far,))))
    return sorted(hops, key=lambda hop: hop.name)


@dataclass(frozen=True, slots=True)
class _Link:
    hop: Hop
    near_key: str
    far_key: str
    title: str | None
    values: Mapping[str, Any]
    key_provenance: str | None = None
    """For a Link backed by a foreign key: provenance of the last change of that key."""

    @property
    def element(self) -> tuple[str, str]:
        """Partition and element key of an explicit Link."""
        forward = self.hop.direction is LinkDirection.FORWARD
        source, target = (self.near_key, self.far_key) if forward else (self.far_key, self.near_key)
        return relation_partition(self.hop.relation.id), link_key(source, target)


async def _links(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    kind: Kind,
    key: str,
    row: Mapping[Any, Any],
    limit: int,
) -> tuple[list[_Link], bool]:
    """The Record's Links by Relation name and far key, at most `limit` (and whether more)."""
    compiler = compiler_for(point, runtime)
    found: list[_Link] = []
    for hop in _hops(point, kind):
        remaining = limit - len(found)
        if remaining < 0:
            break
        source = compiler.link_source(hop)
        far = point.layout.records("far")
        inverse_key = hop.foreign_key and hop.direction is LinkDirection.INVERSE
        holder = source.rows.c.lineage if inverse_key else None
        columns: list[Any] = [
            source.far.label("far_key"),
            source.values.label("link_values"),
            far.c.title,
        ]
        if holder is not None:
            columns.append(holder.label("holder_lineage"))
        query = (
            select(*columns)
            .select_from(source.rows)
            .outerjoin(
                far,
                and_(
                    far.c.domain_id == literal(point.domain.id),
                    far.c.branch == literal(point.layout.branch),
                    far.c.kind_id == literal(hop.far.kind_ids[0]),
                    far.c.key == source.far,
                ),
            )
            .where(source.scope, source.near == literal(key))
            .order_by(source.far)
            .limit(remaining + 1)
        )
        for link_row in (await session.execute(query)).mappings():
            key_provenance = None
            if hop.foreign_key:
                held = (
                    link_row["holder_lineage"]
                    if hop.direction is LinkDirection.INVERSE
                    else row.get("lineage")
                ) or {}
                key_provenance = held.get(hop.relation.foreign_key_attribute or "")
            found.append(
                _Link(
                    hop,
                    key,
                    link_row["far_key"],
                    link_row["title"],
                    link_row["link_values"] or {},
                    key_provenance,
                )
            )
    return found[:limit], len(found) > limit


async def _link_creations(
    session: AsyncSession, point: ReadPoint, elements: Sequence[tuple[str, str]]
) -> dict[tuple[str, str], tuple[bytes, Mapping[str, str] | None]]:
    """Per explicit Link element, the Revision of its latest creation and its provenance."""
    if not elements:
        return {}
    h, r = point.layout.history(), REVISIONS
    ranked = (
        select(
            h.c.partition,
            h.c.element_key,
            h.c.revision_id,
            h.c.provenance,
            func.row_number()
            .over(
                partition_by=(h.c.partition, h.c.element_key),
                order_by=r.c.generation.desc(),
            )
            .label("rank"),
        )
        .join(r, (r.c.domain_id == h.c.domain_id) & (r.c.id == h.c.revision_id))
        .where(
            h.c.domain_id == point.domain.id,
            h.c.branch == point.layout.branch,
            h.c.old_hash.is_(None),
            tuple_(h.c.partition, h.c.element_key).in_(list(elements)),
        )
        .subquery()
    )
    result = await session.execute(select(ranked).where(ranked.c.rank == 1))
    return {
        (row.partition, row.element_key): (bytes(row.revision_id), row.provenance) for row in result
    }


def _first_provenance(provenance: Mapping[str, str] | None) -> str | None:
    if not provenance:
        return None
    return provenance.get(ELEMENT_PROVENANCE_KEY) or provenance[sorted(provenance)[0]]


async def _proposals(
    session: AsyncSession,
    point: ReadPoint,
    revisions: Iterable[bytes],
    resolved: Iterable[str],
    limit: int,
) -> list[dict[str, Any]]:
    if limit == 0:
        return []
    p = PROPOSALS
    ids = list(resolved)
    condition: ColumnElement[bool] = p.c.merged_revision.in_(list(revisions))
    if ids:
        condition = or_(condition, p.c.id.in_(ids))
    result = await session.execute(
        select(p.c.id, p.c.title, p.c.status, p.c.merged_revision)
        .where(p.c.domain_id == point.domain.id, condition)
        .order_by(p.c.created_at, p.c.id)
        .limit(limit)
    )
    return [
        _compact(
            {
                "id": row.id,
                "title": row.title,
                "status": str(row.status),
                "merged_revision": None
                if row.merged_revision is None
                else bytes(row.merged_revision).hex(),
            }
        )
        for row in result
    ]


async def explain_record(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    kind: Kind,
    key: str,
    settings: ExplainSettings,
) -> dict[str, Any]:
    """The explanation document of `kind/key` (see module doc)."""
    point = await with_rows(session, runtime, point, "Explanations")
    fetched = await fetch_record(session, runtime, point, kind, key)
    row = fetched.row
    record = record_out(point.schema, row, masking=point.masking)
    writes = runtime.store_settings.writes
    history = await MaterializedRepository(
        session, point.domain.id, point.layout.branch, writes=writes, layout=point.layout
    ).history(kind_partition(kind.id), key)
    links, truncated = await _links(session, runtime, point, kind, key, row, settings.max_links)
    explicit = [
        link.element for link in links if link.hop.relation.backing is RelationBacking.EXPLICIT
    ]
    creations = await _link_creations(session, point, explicit)

    lineage: Mapping[str, str] = row.get("lineage") or {}
    created_row = next((h for h in reversed(history) if h.old_hash is None), None)
    provenance_ids = [*lineage.values()]
    provenance_ids += [link.key_provenance for link in links if link.key_provenance]
    provenance_ids += [
        p for _, provenance in creations.values() for p in (provenance or {}).values()
    ]
    if created_row is not None:
        provenance_ids += list((created_row.provenance or {}).values())
    entries = await ProvenanceRepository(session, point.domain.id, writes=writes).get_many(
        provenance_ids
    )
    revisions = await _revisions(
        session,
        point.domain.id,
        [h.revision_id for h in history] + [rev for rev, _ in creations.values()],
    )
    # The Revision that set each provenance entry on this Record, newest change winning.
    revision_of: dict[str, bytes] = {}
    for change in history:
        for provenance in (change.provenance or {}).values():
            revision_of[provenance] = change.revision_id

    def origin_of(provenance: str | None, revision: bytes | None = None) -> dict[str, Any]:
        entry = None if provenance is None else entries.get(provenance)
        if revision is None and provenance is not None:
            revision = revision_of.get(provenance)
        return _origin(entry, None if revision is None else revisions.get(revision))

    created = None
    if created_row is not None:
        revision = revisions.get(created_row.revision_id)
        created = origin_of(_first_provenance(created_row.provenance), created_row.revision_id)
        if revision is not None:
            created["revision_origin"] = revision.origin

    values = ordered_values(kind, record.values)
    attributes = []
    for attribute in ordered_attributes(kind):
        if attribute.api_name not in values and attribute.id not in lineage:
            continue
        attributes.append(
            {
                "name": attribute.api_name,
                "value": values.get(attribute.api_name),
                **origin_of(lineage.get(attribute.id)),
            }
        )

    link_out = []
    for link in links:
        hop = link.hop
        far_kind = hop.far.kinds[0].api_name
        if hop.foreign_key:
            since = origin_of(link.key_provenance)
        else:
            created_link = creations.get(link.element)
            since = (
                {}
                if created_link is None
                else origin_of(_first_provenance(created_link[1]), created_link[0])
            )
        entry = {
            "relation": hop.name,
            "direction": str(hop.direction),
            "ref": ref_of(far_kind, link.far_key),
            "title": point.masking.title(point.schema, hop.far.kind_ids[0], link.title),
            "values": ordered_values(
                hop.relation, point.masking.link_values(point.schema, hop.relation, link.values)
            )
            or None,
            **since,
        }
        link_out.append(_compact(entry))

    resolved = [
        str(e.origin_ref.get("proposal_id"))
        for e in entries.values()
        if e.origin_type == MERGE_RESOLUTION and e.origin_ref.get("proposal_id")
    ]
    proposals = await _proposals(
        session, point, [h.revision_id for h in history], resolved, settings.max_proposals
    )
    return {
        "domain": point.domain.id,
        "branch": point.branch,
        "revision": point.revision_hex,
        "record": _compact(
            {
                "ref": ref_of(kind.api_name, key),
                "kind": kind.api_name,
                "key": key,
                "title": record.title,
                "values": values,
                "updated_revision": record.updated_revision,
                "updated_at": timestamp(record.updated_at),
            }
        ),
        "created": created,
        "attributes": attributes,
        "links": link_out,
        "links_truncated": truncated,
        "history": _compact(
            {
                "changes": len(history),
                "first_revision": history[0].revision_id.hex() if history else None,
                "last_revision": history[-1].revision_id.hex() if history else None,
            }
        ),
        "proposals": proposals,
    }


# Markdown -------------------------------------------------------------------------------------


def _source(item: Mapping[str, Any]) -> str:
    origin = item.get("origin") or {}
    kind = item.get("origin_type") or ""
    ref = origin.get("ref") if isinstance(origin, dict) else None
    return f"{kind} {ref}" if kind and ref else kind


def explain_markdown(document: Mapping[str, Any]) -> str:
    """The explanation as compact markdown (see `agents.markdown`)."""
    record = document["record"]
    history = document["history"]
    lines = [
        f"# Explain: {record['ref']}"
        + (f": {text(record['title'])}" if record.get("title") else ""),
        f"{document['domain']}/{document['branch']} at revision {document['revision']}; "
        f"{history['changes']} change{'' if history['changes'] == 1 else 's'} on this branch",
    ]
    sections = ["\n".join(lines)]
    created = document.get("created")
    if created:
        revision_origin = created.get("revision_origin") or {}
        what = " ".join(
            str(part) for part in (revision_origin.get("type"), revision_origin.get("ref")) if part
        )
        sections.append(
            "\n".join(
                [
                    "## Created",
                    f"In {short(created.get('revision'))} at {created.get('at', '')} by "
                    f"{created.get('actor', 'unknown')}"
                    + (f" ({what})" if what else "")
                    + (f": {text(created['message'])}" if created.get("message") else ""),
                ]
            )
        )
    rows = [
        (
            a["name"],
            a.get("value"),
            short(a.get("revision")),
            a.get("at", ""),
            a.get("actor", ""),
            _source(a),
            a.get("message", ""),
        )
        for a in document["attributes"]
    ]
    header = ("attribute", "value", "last change", "at", "by", "origin", "message")
    sections.append("\n".join(["## Attributes", *table(header, rows)]))
    if document["links"]:
        link_rows = [
            (
                link["relation"],
                link["ref"],
                link.get("title", ""),
                pairs(link.get("values")),
                short(link.get("revision")),
                link.get("at", ""),
                link.get("actor", ""),
                _source(link),
            )
            for link in document["links"]
        ]
        link_header = ("relation", "record", "title", "values", "made in", "at", "by", "origin")
        block = ["## Links", *table(link_header, link_rows)]
        if document["links_truncated"]:
            block.append("(more Links not listed)")
        sections.append("\n".join(block))
    if document["proposals"]:
        sections.append(
            "\n".join(
                [
                    "## Proposals",
                    *(
                        f"- {p['id']}: {text(p['title'])} ({p['status']}"
                        + (
                            f", merged in {short(p.get('merged_revision'))}"
                            if p.get("merged_revision")
                            else ""
                        )
                        + ")"
                        for p in document["proposals"]
                    ),
                ]
            )
        )
    return "\n\n".join(sections) + "\n"
