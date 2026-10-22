"""History and lineage of one Record on a branch (design 5.11, 6.3, 6.11).

Both read the branch history table (`mat_history`): one row per change of an element along
the first-parent chain of the branch, keyed by `(domain, branch, partition, element_key,
revision)`, so the changes of one Record are an index range of that key. Neither walks the
Revision graph nor scans changesets: the cost grows with the number of changes of the Record,
not with the length of the branch. Overlay branches read their own rows and their base's up to
the fork point (`BranchLayout.history`).

- History: the Revisions that changed the Record, newest first by generation, paged with a
  `(generation, revision)` keyset. Each change says whether it created, updated or deleted the
  Record, which Attributes it changed (the stored objects before and after are compared) with
  the provenance of each, and on request the values before and after.
- Lineage: per Attribute holding a value, the provenance of its last change
  (`mat_records.lineage`), the Revision that made it, and what the provenance names: the
  Source, the Operation, the extraction evidence (document, span, model) and the Proposal that
  brought the change to the branch (merged in that Revision, or whose conflict resolution set
  the value).

Only Attributes of the Record's own Kind appear, so a caller whose grants are narrowed to Kinds
learns nothing about other Kinds here; derived values are not stored content and have no
history. Both are served at branch heads only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, JsonValue
from sqlalchemy import LargeBinary, Table, and_, func, literal, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Kind, OriginType
from lorebase_core.tree import kind_partition
from lorebase_keeper.reading.cursors import invalid_cursor, issue, position_of
from lorebase_keeper.reading.points import ReadPoint, not_found
from lorebase_keeper.reading.problems import tree_unsupported
from lorebase_keeper.reading.records import fetch_record
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.reading.sensitivity import Masking
from lorebase_keeper.reading.views import Served
from lorebase_store.materialize.rows import ELEMENT_PROVENANCE_KEY
from lorebase_store.models import metadata
from lorebase_store.objectstore import PgObjectStore
from lorebase_store.repositories import ProvenanceRepository
from lorebase_store.repositories.provenance import ProvenanceRow

REVISIONS: Table = metadata.tables["revisions"]
PROPOSALS: Table = metadata.tables["proposals"]


# Response shapes ------------------------------------------------------------------------------


class ChangeKind(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"


class ProvenanceOut(BaseModel):
    """A provenance entry (design 5.11): where a change came from."""

    id: str
    origin_type: str = Field(
        description="`operation`, `source_sync`, `extraction`, `merge_resolution`, "
        "`schema_migration` or `import`."
    )
    origin: dict[str, JsonValue] = Field(description="The origin document of that type.")
    actor: str
    at: datetime


class AttributeChangeOut(BaseModel):
    name: str = Field(description="Attribute api name.")
    before: JsonValue = Field(default=None, description="Value before; with `values=true`.")
    after: JsonValue = Field(default=None, description="Value after; with `values=true`.")
    provenance: ProvenanceOut | None = None


class RecordChangeOut(BaseModel):
    """One Revision that changed the Record on the branch."""

    revision: str
    generation: int
    at: datetime
    author: str
    on_behalf_of: str | None = None
    message: str
    origin: dict[str, JsonValue] = Field(description="Origin of the Revision (`type`, `ref`).")
    change: ChangeKind
    attributes: list[AttributeChangeOut] = Field(
        description="Attributes whose value changed, in schema order."
    )
    provenance: ProvenanceOut | None = Field(
        default=None, description="Provenance of the change as a whole (a deletion, for one)."
    )


class RecordHistoryOut(Served):
    branch: str
    kind: str
    key: str
    items: list[RecordChangeOut]
    next_cursor: str | None = None


class LineageProposalOut(BaseModel):
    id: str
    title: str
    status: str
    merged_revision: str | None = None


class ExtractionEvidenceOut(BaseModel):
    """Where an extracted value was read: the document span and the model that read it."""

    job_id: str
    document_id: str
    span_start: int
    span_end: int
    model_id: str
    prompt_version: str


class ValueLineageOut(BaseModel):
    """Where the current value of one Attribute came from."""

    name: str = Field(description="Attribute api name.")
    value: JsonValue = None
    provenance: ProvenanceOut | None = None
    revision: str | None = Field(default=None, description="Revision of the last change.")
    revision_at: datetime | None = None
    author: str | None = Field(default=None, description="Author of that Revision.")
    message: str | None = None
    source: str | None = Field(default=None, description="Source api name (`source_sync`).")
    operation: str | None = Field(default=None, description="Operation api name (`operation`).")
    extraction: ExtractionEvidenceOut | None = None
    proposal: LineageProposalOut | None = Field(
        default=None,
        description="The Proposal merged in that Revision, or whose conflict resolution set "
        "the value.",
    )


class RecordLineageOut(Served):
    branch: str
    kind: str
    key: str
    created: RecordChangeOut | None = Field(
        default=None, description="The change that last created the Record."
    )
    changes: int = Field(description="Changes of the Record on the branch.")
    attributes: list[ValueLineageOut]
    sources: list[str] = Field(description="Sources behind the current values, by api name.")
    proposals: list[LineageProposalOut] = Field(description="Proposals behind the current values.")


# Loading --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Change:
    revision: bytes
    generation: int
    at: datetime
    author: str
    on_behalf_of: str | None
    message: str
    origin: dict[str, Any]
    old_hash: bytes | None
    new_hash: bytes | None
    provenance: Mapping[str, str]


def _require_materialized(point: ReadPoint, what: str) -> None:
    if not point.materialized:
        raise tree_unsupported(what)


async def _changes(
    session: AsyncSession,
    point: ReadPoint,
    kind: Kind,
    key: str,
    *,
    limit: int | None = None,
    before: tuple[int, bytes] | None = None,
) -> list[_Change]:
    """Changes of the Record on the branch, newest first (at most `limit`)."""
    h, r = point.layout.history(), REVISIONS
    query = (
        select(
            h.c.revision_id,
            r.c.generation,
            r.c.created_at,
            r.c.author,
            r.c.on_behalf_of,
            r.c.message,
            r.c.origin,
            h.c.old_hash,
            h.c.new_hash,
            h.c.provenance,
        )
        .join(r, and_(r.c.domain_id == h.c.domain_id, r.c.id == h.c.revision_id))
        .where(
            h.c.domain_id == point.domain.id,
            h.c.branch == point.branch,
            h.c.partition == kind_partition(kind.id),
            h.c.element_key == key,
        )
        .order_by(r.c.generation.desc(), h.c.revision_id.desc())
    )
    if before is not None:
        generation, revision = before
        query = query.where(
            tuple_(r.c.generation, h.c.revision_id)
            < tuple_(literal(generation), literal(revision, LargeBinary))
        )
    if limit is not None:
        query = query.limit(limit)
    return [
        _Change(
            bytes(row.revision_id),
            row.generation,
            row.created_at,
            row.author,
            row.on_behalf_of,
            row.message,
            row.origin,
            None if row.old_hash is None else bytes(row.old_hash),
            None if row.new_hash is None else bytes(row.new_hash),
            row.provenance or {},
        )
        for row in await session.execute(query)
    ]


async def _provenance(
    session: AsyncSession, runtime: ReadRuntime, domain_id: str, ids: Iterable[str]
) -> dict[str, ProvenanceOut]:
    rows = await ProvenanceRepository(
        session, domain_id, writes=runtime.store_settings.writes
    ).get_many(ids)
    return {id_: provenance_out(row) for id_, row in rows.items()}


def provenance_out(row: ProvenanceRow) -> ProvenanceOut:
    return ProvenanceOut(
        id=row.id,
        origin_type=row.origin_type,
        origin=dict(row.origin_ref),
        actor=row.actor,
        at=row.at,
    )


async def _bodies(
    session: AsyncSession, runtime: ReadRuntime, domain_id: str, hashes: Iterable[bytes | None]
) -> dict[bytes, Mapping[str, Any]]:
    settings = runtime.store_settings
    store = PgObjectStore(
        session, domain_id, writes=settings.writes, batch=settings.materialize.read_batch_rows
    )
    found = await store.get_many(h for h in hashes if h is not None)
    return {digest: stored.body.get("values") or {} for digest, stored in found.items()}


def _change_kind(change: _Change) -> ChangeKind:
    if change.old_hash is None:
        return ChangeKind.CREATED
    if change.new_hash is None:
        return ChangeKind.DELETED
    return ChangeKind.UPDATED


def _change_out(
    kind: Kind,
    change: _Change,
    bodies: Mapping[bytes, Mapping[str, Any]],
    provenance: Mapping[str, ProvenanceOut],
    *,
    values: bool,
    masking: Masking,
) -> RecordChangeOut:
    order = _reading_order(kind)
    scope = (kind.id,)
    old = bodies.get(change.old_hash, {}) if change.old_hash is not None else {}
    new = bodies.get(change.new_hash, {}) if change.new_hash is not None else {}
    attributes = []
    for attribute_id, attribute in kind.attributes.items():
        if attribute.is_derived:
            continue
        before, after = old.get(attribute_id), new.get(attribute_id)
        if before == after:
            continue
        provenance_id = change.provenance.get(attribute_id)
        attributes.append(
            AttributeChangeOut(
                name=attribute.api_name,
                before=masking.value(attribute, scope, before) if values else None,
                after=masking.value(attribute, scope, after) if values else None,
                provenance=None if provenance_id is None else provenance.get(provenance_id),
            )
        )
    element = change.provenance.get(ELEMENT_PROVENANCE_KEY)
    return RecordChangeOut(
        revision=change.revision.hex(),
        generation=change.generation,
        at=change.at,
        author=change.author,
        on_behalf_of=change.on_behalf_of,
        message=change.message,
        origin=change.origin,
        change=_change_kind(change),
        attributes=sorted(attributes, key=lambda a: order(a.name)),
        provenance=None if element is None else provenance.get(element),
    )


def _reading_order(kind: Kind) -> Callable[[str], tuple[int, str]]:
    """Sort key of Attribute api names: key Attributes, the title Attribute, then by name."""
    first = [*kind.primary_key, *([kind.title_attribute] if kind.title_attribute else [])]
    rank: dict[str, int] = {}
    for attribute_id in first:
        if attribute_id in kind.attributes:
            rank.setdefault(kind.attributes[attribute_id].api_name, len(rank))
    return lambda name: (rank.get(name, len(rank)), name)


async def _render(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    kind: Kind,
    changes: Sequence[_Change],
    *,
    values: bool,
) -> list[RecordChangeOut]:
    domain = point.domain.id
    bodies = await _bodies(
        session, runtime, domain, [h for c in changes for h in (c.old_hash, c.new_hash)]
    )
    provenance = await _provenance(
        session, runtime, domain, [p for c in changes for p in c.provenance.values()]
    )
    return [
        _change_out(kind, c, bodies, provenance, values=values, masking=point.masking)
        for c in changes
    ]


# History --------------------------------------------------------------------------------------


async def record_history(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    kind: Kind,
    key: str,
    *,
    limit: int,
    cursor: str | None,
    values: bool,
    shape: str,
) -> RecordHistoryOut:
    """A page of the changes of `kind/key` on the branch, newest first (see module doc)."""
    _require_materialized(point, "Record history")
    position = position_of(cursor, shape, "/cursor")
    before = None
    if position is not None:
        try:
            generation, revision = int(position[0]), bytes.fromhex(str(position[1]))
        except (IndexError, TypeError, ValueError):
            raise invalid_cursor("/cursor") from None
        before = (generation, revision)
    changes = await _changes(session, point, kind, key, limit=limit + 1, before=before)
    if not changes and position is None:
        raise not_found(f"Record {kind.api_name}/{key}")
    more = len(changes) > limit
    changes = changes[:limit]
    items = await _render(session, runtime, point, kind, changes, values=values)
    last = changes[-1] if changes else None
    return RecordHistoryOut(
        revision=point.revision_hex,
        served_from=point.served_from,
        branch=point.branch,
        kind=kind.api_name,
        key=key,
        items=items,
        next_cursor=issue(shape, [last.generation, last.revision.hex()])
        if more and last is not None
        else None,
    )


# Lineage --------------------------------------------------------------------------------------


async def _proposals(
    session: AsyncSession, domain_id: str, revisions: Iterable[bytes], ids: Iterable[str]
) -> tuple[dict[bytes, LineageProposalOut], dict[str, LineageProposalOut]]:
    """Proposals merged in `revisions`, and the Proposals named by `ids`."""
    merged, named = list(dict.fromkeys(revisions)), list(dict.fromkeys(ids))
    if not merged and not named:
        return {}, {}
    p = PROPOSALS
    result = await session.execute(
        select(p.c.id, p.c.title, p.c.status, p.c.merged_revision)
        .where(
            p.c.domain_id == domain_id,
            or_(p.c.merged_revision.in_(merged), p.c.id.in_(named)),
        )
        .order_by(p.c.created_at, p.c.id)
    )
    by_revision: dict[bytes, LineageProposalOut] = {}
    by_id: dict[str, LineageProposalOut] = {}
    for row in result:
        merged_revision = None if row.merged_revision is None else bytes(row.merged_revision)
        out = LineageProposalOut(
            id=row.id,
            title=row.title,
            status=str(row.status),
            merged_revision=None if merged_revision is None else merged_revision.hex(),
        )
        by_id[row.id] = out
        if merged_revision is not None:
            by_revision.setdefault(merged_revision, out)
    return by_revision, by_id


async def _count(session: AsyncSession, point: ReadPoint, kind: Kind, key: str) -> int:
    h = point.layout.history()
    count = await session.scalar(
        select(func.count()).where(
            h.c.domain_id == point.domain.id,
            h.c.branch == point.branch,
            h.c.partition == kind_partition(kind.id),
            h.c.element_key == key,
        )
    )
    return int(count or 0)


async def record_lineage(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    kind: Kind,
    key: str,
    *,
    max_changes: int,
) -> RecordLineageOut:
    """Where the current values of `kind/key` came from (see module doc).

    The newest `max_changes` changes of the Record are read to find the Revision behind each
    value; a value last changed before them keeps its provenance but names no Revision.
    """
    _require_materialized(point, "Record lineage")
    row = (await fetch_record(session, runtime, point, kind, key)).row
    changes = await _changes(session, point, kind, key, limit=max_changes)
    lineage: Mapping[str, str] = row.get("lineage") or {}
    domain = point.domain.id
    created_change = next((c for c in changes if c.old_hash is None), None)
    provenance = await _provenance(
        session,
        runtime,
        domain,
        [
            *lineage.values(),
            *((created_change.provenance.values()) if created_change is not None else ()),
        ],
    )
    # The newest change that recorded each provenance entry on this Record.
    change_of: dict[str, _Change] = {}
    for change in changes:
        for provenance_id in change.provenance.values():
            change_of.setdefault(provenance_id, change)
    resolved = [
        str(entry.origin["proposal_id"])
        for entry in provenance.values()
        if entry.origin_type == OriginType.MERGE_RESOLUTION and entry.origin.get("proposal_id")
    ]
    by_revision, by_id = await _proposals(
        session,
        domain,
        [change_of[p].revision for p in lineage.values() if p in change_of],
        resolved,
    )
    sources = {source.id: source.api_name for source in point.schema.sources.values()}

    values: Mapping[str, Any] = row.get("values") or {}
    attributes: list[ValueLineageOut] = []
    for attribute_id, attribute in kind.attributes.items():
        if attribute.is_derived or (attribute_id not in values and attribute_id not in lineage):
            continue
        held = lineage.get(attribute_id)
        entry = None if held is None else provenance.get(held)
        setter = None if held is None else change_of.get(held)
        origin: Mapping[str, Any] = {} if entry is None else entry.origin
        origin_type = None if entry is None else entry.origin_type
        proposal = None
        if origin_type == OriginType.MERGE_RESOLUTION:
            proposal = by_id.get(str(origin.get("proposal_id")))
        if proposal is None and setter is not None:
            proposal = by_revision.get(setter.revision)
        source = operation = None
        extraction = None
        if origin_type == OriginType.SOURCE_SYNC:
            source = sources.get(str(origin.get("source_id")))
        elif origin_type == OriginType.OPERATION and origin.get("operation"):
            operation = str(origin["operation"])
        elif origin_type == OriginType.EXTRACTION:
            extraction = ExtractionEvidenceOut.model_validate(origin)
        attributes.append(
            ValueLineageOut(
                name=attribute.api_name,
                value=point.masking.value(attribute, (kind.id,), values.get(attribute_id)),
                provenance=entry,
                revision=None if setter is None else setter.revision.hex(),
                revision_at=None if setter is None else setter.at,
                author=None if setter is None else setter.author,
                message=None if setter is None else setter.message,
                source=source,
                operation=operation,
                extraction=extraction,
                proposal=proposal,
            )
        )
    order = _reading_order(kind)
    attributes.sort(key=lambda a: order(a.name))
    created = None
    if created_change is not None:
        rendered = await _render(session, runtime, point, kind, [created_change], values=False)
        created = rendered[0]
    proposals = {p.id: p for a in attributes if (p := a.proposal) is not None}
    return RecordLineageOut(
        revision=point.revision_hex,
        served_from=point.served_from,
        branch=point.branch,
        kind=kind.api_name,
        key=key,
        created=created,
        changes=len(changes)
        if len(changes) < max_changes
        else await _count(session, point, kind, key),
        attributes=attributes,
        sources=sorted({a.source for a in attributes if a.source is not None}),
        proposals=list(proposals.values()),
    )
