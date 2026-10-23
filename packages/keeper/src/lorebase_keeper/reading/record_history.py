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
  (`mat_records.lineage`), the Revision that made it (`mat_records.lineage_revisions`), and
  what the provenance names: the Source, the Operation, the extraction evidence (document,
  span, model) and the Proposal that brought the change to the branch (merged in that
  Revision, or whose conflict resolution set the value), read in one statement.

Only Attributes of the Record's own Kind appear, so a caller whose grants are narrowed to Kinds
learns nothing about other Kinds here; derived values are not stored content and have no
history. History and lineage are served at branch heads and, through a snapshot of the
Revision (`reading.snapshots`), at other Revisions: what the head answered when the Revision
was the head (the changes along its first-parent chain).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, JsonValue
from sqlalchemy import (
    LargeBinary,
    Table,
    TextClause,
    and_,
    literal,
    select,
    text,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Kind, OriginType
from lorebase_core.tree import kind_partition
from lorebase_keeper.reading.cursors import invalid_cursor, issue, position_of
from lorebase_keeper.reading.points import ReadPoint, not_found
from lorebase_keeper.reading.problems import tree_unsupported
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.reading.sensitivity import Masking
from lorebase_keeper.reading.snapshots import with_rows
from lorebase_keeper.reading.views import Served
from lorebase_store.materialize.rows import CREATED_KEY, ELEMENT_PROVENANCE_KEY
from lorebase_store.models import metadata
from lorebase_store.objectstore import PgObjectStore
from lorebase_store.repositories import ProvenanceRepository
from lorebase_store.repositories.provenance import ProvenanceRow

REVISIONS: Table = metadata.tables["revisions"]


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
    changes: int = Field(
        description="Changes of the Record on the branch, at most `read.history.lineage_count_max`."
    )
    changes_truncated: bool = Field(
        default=False,
        description="The Record has more changes than `changes` (the count stopped at "
        "`read.history.lineage_count_max`).",
    )
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
            h.c.branch == point.layout.branch,
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


def _nothing(_value: Any) -> bool:
    return False


async def _provenance(
    session: AsyncSession, point: ReadPoint, runtime: ReadRuntime, ids: Iterable[str]
) -> dict[str, ProvenanceOut]:
    rows = await ProvenanceRepository(
        session, point.domain.id, writes=runtime.store_settings.writes
    ).get_many(ids)
    return {id_: provenance_out(row, point.tells) for id_, row in rows.items()}


def provenance_out(row: ProvenanceRow, tells: Callable[[Any], bool] = _nothing) -> ProvenanceOut:
    """A provenance entry; its origin document is left out when it names something invisible
    to the caller (`ReadPoint.tells`, an Operation or Source left out of its schema)."""
    origin = dict(row.origin_ref)
    return ProvenanceOut(
        id=row.id,
        origin_type=row.origin_type,
        origin={} if tells(origin) else origin,
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
    tells: Callable[[Any], bool] = _nothing,
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
        message="" if tells(change.message) else change.message,
        origin={} if tells(change.origin) else change.origin,
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
        session, point, runtime, [p for c in changes for p in c.provenance.values()]
    )
    items = [
        _change_out(
            kind, c, bodies, provenance, values=values, masking=point.masking, tells=point.tells
        )
        for c in changes
    ]
    if not point.hidden:
        return items
    # A change of nothing but Attributes left out of the caller's schema (a reference to an
    # invisible Kind) would tell that, and when, something invisible changed.
    return [i for i in items if i.change is not ChangeKind.UPDATED or i.attributes]


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
    position = position_of(cursor, shape, "/cursor")
    before = None
    if position is not None:
        try:
            generation, revision = int(position[0]), bytes.fromhex(str(position[1]))
        except (IndexError, TypeError, ValueError):
            raise invalid_cursor("/cursor") from None
        before = (generation, revision)
    point = await with_rows(session, runtime, point, "Record history")
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

# Per Attribute of the lineage, its provenance entry and the Revision of its last change, at the
# branch head: from `mat_records.lineage_revisions`. For rows that do not name them (written
# before they were kept and not yet backfilled), the newest `:scan` changes of the Record are
# read once (`recent`, skipped when the row names everything) and searched: the newest change
# that recorded that entry for the Attribute, and the newest creation. The change that created
# the Record is the history row of `$created`, or that fallback when the row is missing. JSON
# that is not an object (null lineage, for one) reads as empty. Changes are counted up to
# `:count`, so a Record with a long history costs no more than that bound. Shared lineage maps
# (revision 0036) are resolved with `lb_lineage`.
_HEAD = """
rec AS MATERIALIZED (
    SELECT m."values" AS vals,
           CASE WHEN jsonb_typeof(m.lineage) = 'object' THEN lb_lineage(m.domain_id, m.lineage)
                ELSE '{{}}'::jsonb END AS lineage,
           CASE WHEN jsonb_typeof(m.lineage_revisions) = 'object'
                THEN lb_lineage(m.domain_id, m.lineage_revisions) END AS revs
    FROM {records}
    WHERE m.domain_id = :domain AND m.branch = :branch AND m.kind_id = :kind AND m.key = :key
),
created_direct AS MATERIALIZED (
    SELECT h.revision_id, h.old_hash, h.new_hash, h.provenance
    FROM rec
    JOIN {history_h}
      ON h.domain_id = :domain AND h.branch = :branch AND h.partition = :partition
     AND h.element_key = :key AND h.revision_id = decode(rec.revs ->> '{created_key}', 'hex')
    LIMIT 1
),
recent AS MATERIALIZED (
    SELECT h.revision_id, h.old_hash, h.new_hash, h.provenance, r.generation
    FROM {history_h}
    JOIN revisions r ON r.domain_id = h.domain_id AND r.id = h.revision_id
    WHERE h.domain_id = :domain AND h.branch = :branch AND h.partition = :partition
      AND h.element_key = :key
      AND EXISTS (SELECT 1 FROM rec)
      AND (NOT EXISTS (SELECT 1 FROM created_direct)
           OR EXISTS (
             SELECT 1 FROM rec CROSS JOIN LATERAL jsonb_object_keys(rec.lineage) AS k(attribute)
             WHERE (rec.revs ->> k.attribute) IS NULL))
    ORDER BY r.generation DESC, h.revision_id DESC
    LIMIT :scan
),
lin AS (
    SELECT l.attribute, l.provenance_id,
           COALESCE(
             decode(rec.revs ->> l.attribute, 'hex'),
             (SELECT s.revision_id FROM recent s
              WHERE s.provenance ->> l.attribute = l.provenance_id
              ORDER BY s.generation DESC, s.revision_id DESC
              LIMIT 1)) AS revision_id
    FROM rec
    CROSS JOIN LATERAL jsonb_each_text(rec.lineage) AS l(attribute, provenance_id)
),
created AS (
    SELECT revision_id, old_hash, new_hash, provenance FROM created_direct
    UNION ALL
    SELECT c.revision_id, c.old_hash, c.new_hash, c.provenance
    FROM (
        SELECT s.revision_id, s.old_hash, s.new_hash, s.provenance
        FROM recent s
        WHERE s.old_hash IS NULL AND NOT EXISTS (SELECT 1 FROM created_direct)
        ORDER BY s.generation DESC, s.revision_id DESC
        LIMIT 1
    ) AS c
),
counted AS (
    SELECT count(*) AS changes
    FROM (
        SELECT 1
        FROM {history_h}
        WHERE h.domain_id = :domain AND h.branch = :branch AND h.partition = :partition
          AND h.element_key = :key
        LIMIT :count
    ) AS c
)
"""

# What the lineage names, in one row: the Revisions, provenance entries and Proposals behind
# the values, the change that created the Record (with the values it wrote) and the count of
# changes.
_TAIL = """
SELECT
    {found} AS found,
    {values} AS vals,
    (SELECT jsonb_agg(jsonb_build_object(
        'attribute', attribute, 'provenance', provenance_id,
        'revision', encode(revision_id, 'hex')))
     FROM lin) AS lineage,
    (SELECT jsonb_agg(jsonb_build_object(
        'id', encode(r.id, 'hex'), 'generation', r.generation, 'at', r.created_at,
        'author', r.author, 'on_behalf_of', r.on_behalf_of, 'message', r.message,
        'origin', r.origin))
     FROM revisions r
     WHERE r.domain_id = :domain
       AND r.id IN (SELECT revision_id FROM lin UNION SELECT revision_id FROM created)
    ) AS revisions,
    (SELECT jsonb_agg(jsonb_build_object(
        'id', p.id, 'origin_type', p.origin_type, 'origin', p.origin_ref, 'actor', p.actor,
        'at', p.at))
     FROM provenance p
     WHERE p.domain_id = :domain
       AND p.id IN (
         SELECT provenance_id FROM lin
         UNION SELECT e.value FROM created,
           jsonb_each_text({provenance_object}) AS e)
    ) AS provenance,
    (SELECT jsonb_agg(jsonb_build_object(
        'id', q.id, 'title', q.title, 'status', q.status,
        'merged_revision', encode(q.merged_revision, 'hex')) ORDER BY q.created_at, q.id)
     FROM proposals q
     WHERE q.domain_id = :domain
       AND (q.merged_revision IN (SELECT revision_id FROM lin)
            OR q.id IN (
              SELECT p.origin_ref ->> 'proposal_id' FROM provenance p
              WHERE p.domain_id = :domain AND p.origin_type = '{merge_resolution}'
                AND p.id IN (SELECT provenance_id FROM lin)))
    ) AS proposals,
    (SELECT jsonb_build_object(
        'revision', encode(c.revision_id, 'hex'), 'new_hash', encode(c.new_hash, 'hex'),
        'provenance', {created_provenance}, 'values', o.body -> 'values')
     FROM created c
     LEFT JOIN objects o ON o.domain_id = :domain AND o.hash = c.new_hash
     LIMIT 1) AS created,
    (SELECT changes FROM counted) AS changes
"""


def _object(expression: str) -> str:
    """`expression` when it is a JSON object, else an empty one (null and other JSON)."""
    return (
        f"(CASE WHEN jsonb_typeof({expression}) = 'object' THEN {expression} "
        "ELSE '{}'::jsonb END)"
    )


def _lineage_statement(point: ReadPoint) -> TextClause:
    layout = point.layout
    ctes = _HEAD.format(
        records=layout.records_from("m"),
        history_h=layout.history_from("h"),
        created_key=CREATED_KEY,
    )
    tail = _TAIL.format(
        found="EXISTS (SELECT 1 FROM rec)",
        values="(SELECT vals FROM rec)",
        merge_resolution=OriginType.MERGE_RESOLUTION.value,
        provenance_object=_object("created.provenance"),
        created_provenance=_object("c.provenance"),
    )
    return text(f"WITH {ctes.strip()}{tail}")


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


async def record_lineage(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    kind: Kind,
    key: str,
) -> RecordLineageOut:
    """Where the values of `kind/key` at the read point came from (see module doc): one
    statement over the head's rows, or a snapshot's at another Revision."""
    point = await with_rows(session, runtime, point, "Record lineage")
    settings = runtime.read.history
    params = {
        "domain": point.domain.id,
        "branch": point.layout.branch,
        "kind": kind.id,
        "partition": kind_partition(kind.id),
        "key": key,
        "scan": settings.lineage_scan_changes,
        "count": settings.lineage_count_max + 1,
    }
    row = (await session.execute(_lineage_statement(point), params)).mappings().one()
    if not row["found"]:
        raise not_found(f"Record {kind.api_name}/{key}")
    values: Mapping[str, Any] = row["vals"] or {}
    lineage = {item["attribute"]: item for item in row["lineage"] or ()}
    revisions = {item["id"]: item for item in row["revisions"] or ()}
    provenance = {
        item["id"]: ProvenanceOut(
            id=item["id"],
            origin_type=item["origin_type"],
            origin={} if point.tells(item["origin"]) else item["origin"],
            actor=item["actor"],
            at=_utc(item["at"]),
        )
        for item in row["provenance"] or ()
    }
    by_revision: dict[str, LineageProposalOut] = {}
    by_id: dict[str, LineageProposalOut] = {}
    for item in row["proposals"] or ():
        out = LineageProposalOut.model_validate(item)
        if point.tells(out.title):
            out.title = ""
        by_id[out.id] = out
        if out.merged_revision is not None:
            by_revision.setdefault(out.merged_revision, out)
    sources = {source.id: source.api_name for source in point.schema.sources.values()}

    attributes: list[ValueLineageOut] = []
    for attribute_id, attribute in kind.attributes.items():
        if attribute.is_derived or (attribute_id not in values and attribute_id not in lineage):
            continue
        held = lineage.get(attribute_id)
        entry = None if held is None else provenance.get(held["provenance"])
        setter = None if held is None else revisions.get(held["revision"])
        origin: Mapping[str, Any] = {} if entry is None else entry.origin
        origin_type = None if entry is None else entry.origin_type
        proposal = None
        if origin_type == OriginType.MERGE_RESOLUTION:
            proposal = by_id.get(str(origin.get("proposal_id")))
        if proposal is None and setter is not None:
            proposal = by_revision.get(setter["id"])
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
                revision=None if setter is None else setter["id"],
                revision_at=None if setter is None else _utc(setter["at"]),
                author=None if setter is None else setter["author"],
                message=None
                if setter is None or point.tells(setter["message"])
                else setter["message"],
                source=source,
                operation=operation,
                extraction=extraction,
                proposal=proposal,
            )
        )
    order = _reading_order(kind)
    attributes.sort(key=lambda a: order(a.name))
    proposals = {p.id: p for a in attributes if (p := a.proposal) is not None}
    changes = int(row["changes"] or 0)
    return RecordLineageOut(
        revision=point.revision_hex,
        served_from=point.served_from,
        branch=point.branch,
        kind=kind.api_name,
        key=key,
        created=_created(kind, point, row["created"], revisions, provenance),
        changes=min(changes, settings.lineage_count_max),
        changes_truncated=changes > settings.lineage_count_max,
        attributes=attributes,
        sources=sorted({a.source for a in attributes if a.source is not None}),
        proposals=list(proposals.values()),
    )


def _created(
    kind: Kind,
    point: ReadPoint,
    created: Mapping[str, Any] | None,
    revisions: Mapping[str, Mapping[str, Any]],
    provenance: Mapping[str, ProvenanceOut],
) -> RecordChangeOut | None:
    """The change that last created the Record, rendered like a history entry."""
    if created is None or (revision := revisions.get(created["revision"])) is None:
        return None
    new_hash = bytes.fromhex(created["new_hash"])
    change = _Change(
        bytes.fromhex(created["revision"]),
        int(revision["generation"]),
        _utc(revision["at"]),
        revision["author"],
        revision["on_behalf_of"],
        revision["message"],
        revision["origin"],
        None,
        new_hash,
        created["provenance"] or {},
    )
    bodies = {new_hash: created["values"] or {}}
    return _change_out(
        kind, change, bodies, provenance, values=False, masking=point.masking, tells=point.tells
    )
