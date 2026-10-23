"""`POST /context`: one call that returns what an agent needs about some Records.

Seeds come from `refs`, a DSL `query` or a lexical `search`. Around them the materialized
head gives the neighborhood up to `depth` (one statement, `lorebase_store.traverse`, walking
only Relations between Kinds the caller may read),
the schema fragment of what is present, the recent changes of those Records and Links (the
branch history, `mat_history`, which mirrors the first-parent changesets) and the lineage of
the seed values. The pack is then trimmed to the token budget (`agents.budget`) and rendered
as JSON or markdown. No model is called and every ordering is fixed, so a request at a given
Revision always yields the same bytes; packs are cached by Revision like GET reads.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Kind, LinkDirection, Relation, Schema
from lorebase_core.tree import kind_partition, link_key, relation_partition
from lorebase_keeper.agents.budget import (
    ChangeItem,
    Draft,
    LineageItem,
    LinkItem,
    NeighborItem,
    SeedItem,
    fit,
)
from lorebase_keeper.agents.markdown import markdown_measure, markdown_sections, pack_markdown
from lorebase_keeper.agents.pack import (
    ContextRequest,
    budget_json,
    dumps,
    json_measure,
    json_sections,
    ordered_attributes,
    ordered_values,
)
from lorebase_keeper.agents.settings import ContextSettings
from lorebase_keeper.reading.compiler import record_columns
from lorebase_keeper.reading.history import REVISIONS
from lorebase_keeper.reading.points import ReadPoint, not_found
from lorebase_keeper.reading.problems import invalid
from lorebase_keeper.reading.records import plan_query, require_kind, run_query
from lorebase_keeper.reading.requests import SearchRequest
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.reading.search import search
from lorebase_keeper.reading.sensitivity import deferred_markdown
from lorebase_keeper.reading.snapshots import with_rows
from lorebase_keeper.reading.views import RecordOut, link_values, record_out
from lorebase_store.materialize.rows import ELEMENT_PROVENANCE_KEY, PartitionType, parse_partition
from lorebase_store.reads import Direction
from lorebase_store.repositories import ProvenanceRepository
from lorebase_store.traverse import TraversalLimitError, neighborhood

MARKDOWN = "text/markdown; charset=utf-8"
JSON = "application/json"


def ref_of(kind: str, key: str) -> str:
    return f"{kind}/{key}"


def timestamp(value: datetime | None) -> str | None:
    """RFC 3339 UTC to the second: compact and stable."""
    return None if value is None else value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class PackSettings:
    budget_tokens: int
    depth: int


def effective(
    request: ContextRequest, settings: ContextSettings, runtime: ReadRuntime
) -> PackSettings:
    """Budget and depth of a request, within the configured bounds (422 otherwise)."""
    budget = request.budget_tokens or settings.default_budget_tokens
    if budget > settings.max_budget_tokens:
        raise invalid(
            "/body/budget_tokens", f"must not exceed {settings.max_budget_tokens}", "limit_exceeded"
        )
    depth = settings.default_depth if request.depth is None else request.depth
    deepest = min(
        settings.max_depth,
        runtime.read.traverse.max_depth,
        runtime.store_settings.traverse.max_depth,
    )
    if depth > deepest:
        raise invalid("/body/depth", f"must not exceed {deepest}", "limit_exceeded")
    return PackSettings(budget, depth)


# Seeds ----------------------------------------------------------------------------------------


async def seed_addresses(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    request: ContextRequest,
    settings: ContextSettings,
) -> list[tuple[Kind, str]]:
    """`(Kind, key)` of the seeds, in request, query or ranking order, without repeats."""
    cap = settings.max_seeds
    found: list[tuple[str, str]] = []
    if request.refs is not None:
        if len(request.refs) > cap:
            raise invalid("/body/refs", f"at most {cap} references", "limit_exceeded")
        for index, ref in enumerate(request.refs):
            kind, sep, key = ref.partition("/")
            if not sep or not kind or not key:
                raise invalid(f"/body/refs/{index}", "must be `Kind/key`", "invalid_ref")
            found.append((kind, key))
    elif request.query is not None:
        limit = min(request.query.limit or cap, cap)
        query = request.query.model_copy(
            update={"limit": limit, "cursor": None, "include": {}, "aggregate": None}
        )
        plan = plan_query(query, point, runtime, "/body/query")
        page = await run_query(
            session, runtime, point, plan, shape="context", cursor_pointer="/body/query/cursor"
        )
        found = [(item.kind, item.key) for item in page.items]
    else:
        assert request.search is not None
        limit = min(cap, runtime.search.results.max_limit)
        hits = await search(
            session, runtime, point, SearchRequest(q=request.search, mode="lexical", limit=limit)
        )
        found = [(hit.kind, hit.key) for hit in hits.items]
    unique = list(dict.fromkeys(found))
    return [(require_kind(point, kind), key) for kind, key in unique]


async def fetch_rows(
    session: AsyncSession, point: ReadPoint, addresses: Sequence[tuple[Kind, str]]
) -> list[Mapping[Any, Any]]:
    """Materialized rows of `addresses`, in their order; 404 for a missing Record."""
    by_kind: dict[str, list[str]] = defaultdict(list)
    for kind, key in addresses:
        by_kind[kind.id].append(key)
    r = point.layout.records()
    rows: dict[tuple[str, str], Mapping[Any, Any]] = {}
    for kind_id, keys in by_kind.items():
        result = await session.execute(
            select(*record_columns(r)).where(
                r.c.domain_id == point.domain.id,
                r.c.branch == point.layout.branch,
                r.c.kind_id == kind_id,
                r.c.key.in_(keys),
            )
        )
        for row in result.mappings():
            rows[(kind_id, row["key"])] = row
    out: list[Mapping[Any, Any]] = []
    for kind, key in addresses:
        found = rows.get((kind.id, key))
        if found is None:
            raise not_found(f"Record {kind.api_name}/{key}")
        out.append(found)
    return out


# Neighborhood ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Surroundings:
    neighbors: tuple[NeighborItem, ...]
    links: tuple[LinkItem, ...]
    capped: bool


def _depths(seeds: Iterable[str], links: Iterable[LinkItem]) -> dict[str, int]:
    """Hops from the nearest seed over Links in either direction."""
    adjacent: dict[str, set[str]] = defaultdict(set)
    for link in links:
        adjacent[link.source].add(link.target)
        adjacent[link.target].add(link.source)
    depth = dict.fromkeys(seeds, 0)
    queue = deque(depth)
    while queue:
        node = queue.popleft()
        for other in sorted(adjacent[node]):
            if other not in depth:
                depth[other] = depth[node] + 1
                queue.append(other)
    return depth


async def surroundings(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    seeds: Sequence[RecordOut],
    request: ContextRequest,
    settings: ContextSettings,
    depth: int,
) -> Surroundings:
    if depth == 0 or not seeds:
        return Surroundings((), (), False)
    caps, walk = runtime.read.traverse, runtime.store_settings.traverse
    try:
        found = await neighborhood(
            session,
            point.domain.id,
            point.layout,
            point.schema,
            [(require_kind(point, s.kind).id, s.key) for s in seeds],
            depth=depth,
            follow=_follow(point, request.relations),
            max_nodes=min(len(seeds) + settings.max_neighbors, caps.max_nodes, walk.max_nodes),
            max_links=max(min(settings.max_links, caps.max_edges, walk.max_links), 1),
            runtime=runtime.store,
        )
    except TraversalLimitError as error:
        raise invalid("/body", str(error), "limit_exceeded") from None
    schema = point.schema
    links: dict[tuple[str, str, str], LinkItem] = {}
    for link in found.links:
        relation = schema.relations[link.relation_id]
        source = ref_of(schema.kinds[relation.from_kind].api_name, link.from_key)
        target = ref_of(schema.kinds[relation.to_kind].api_name, link.to_key)
        links.setdefault(
            (relation.api_name, source, target),
            LinkItem(
                relation.api_name,
                source,
                target,
                ordered_values(
                    relation, link_values(point.schema, relation, link.values, point.masking)
                ),
            ),
        )
    seed_refs = [ref_of(s.kind, s.key) for s in seeds]
    ordered_links = tuple(links[k] for k in sorted(links))
    depths = _depths(seed_refs, ordered_links)
    seeded = set(seed_refs)
    neighbors = []
    for reached in found.records:
        record = reached.record
        kind = schema.kinds[record.kind_id]
        ref = ref_of(kind.api_name, record.key)
        if ref in seeded:
            continue
        neighbors.append(
            NeighborItem(
                ref=ref,
                kind=kind.api_name,
                title=point.masking.title(schema, record.kind_id, record.title),
                depth=depths.get(ref, depth),
                values=ordered_values(kind, point.masking.kind_values(kind, record.values)),
            )
        )
    neighbors.sort(key=lambda n: (n.depth, n.kind, n.ref))
    return Surroundings(tuple(neighbors), ordered_links, found.truncated)


def _follow(point: ReadPoint, names: list[str] | None) -> dict[str, Direction]:
    """Relations to walk (id to direction): those named (forward or inverse api name; default:
    all), in the directions named, between Kinds the caller may read."""
    schema = point.schema
    wanted: dict[str, set[LinkDirection]] = defaultdict(set)
    if names is None:
        for relation in schema.relations.values():
            wanted[relation.id] = set(LinkDirection)
    else:
        for index, name in enumerate(names):
            found = schema.relation_by_api_name(name)
            if found is None:
                raise invalid(f"/body/relations/{index}", f"unknown Relation {name!r}", "unknown")
            wanted[found[0].id].add(found[1])
    follow: dict[str, Direction] = {}
    for relation_id, directions in sorted(wanted.items()):
        relation = schema.relations[relation_id]
        ends = (schema.kinds.get(relation.from_kind), schema.kinds.get(relation.to_kind))
        if any(kind is None or not point.scope.allows(kind.api_name) for kind in ends):
            continue  # Kinds the caller may not read are neither returned nor walked through
        if len(directions) == 2:
            follow[relation_id] = Direction.BOTH
        else:
            follow[relation_id] = (
                Direction.OUT if LinkDirection.FORWARD in directions else Direction.IN
            )
    return follow


# History and lineage --------------------------------------------------------------------------


def _change_kind(old: bytes | None, new: bytes | None) -> str:
    if old is None:
        return "added"
    return "removed" if new is None else "modified"


def _attribute_names(
    schema: Schema, partition: str, provenance: Mapping[str, str] | None
) -> tuple[str, ...]:
    parsed = parse_partition(partition)
    owner: Kind | Relation | None = None
    if parsed.type is PartitionType.KIND:
        owner = schema.kinds.get(parsed.element_id)
    elif parsed.type is PartitionType.RELATION:
        owner = schema.relations.get(parsed.element_id)
    if owner is None:
        return ()
    changed = set(provenance or {}) - {ELEMENT_PROVENANCE_KEY}
    return tuple(a.api_name for a in ordered_attributes(owner) if a.id in changed)


async def recent_changes(
    session: AsyncSession,
    point: ReadPoint,
    records: Sequence[tuple[Kind, str]],
    links: Sequence[LinkItem],
    *,
    floor: int | None,
    limit: int,
) -> tuple[ChangeItem, ...]:
    """Changes of the given Records and Links on the branch, newest `limit`, oldest first."""
    elements: dict[tuple[str, str], str] = {}
    for kind, key in records:
        elements[(kind_partition(kind.id), key)] = ref_of(kind.api_name, key)
    for link in links:
        named = point.schema.relation_by_api_name(link.relation)
        if named is None:
            continue
        source_key, target_key = link.source.partition("/")[2], link.target.partition("/")[2]
        elements[(relation_partition(named[0].id), link_key(source_key, target_key))] = (
            f"{link.relation}: {link.source} -> {link.target}"
        )
    if not elements:
        return ()
    h, r = point.layout.history(), REVISIONS
    query = (
        select(
            h.c.partition,
            h.c.element_key,
            h.c.revision_id,
            h.c.at,
            h.c.old_hash,
            h.c.new_hash,
            h.c.provenance,
            r.c.author,
            r.c.message,
            r.c.origin,
        )
        .join(r, (r.c.domain_id == h.c.domain_id) & (r.c.id == h.c.revision_id))
        .where(
            h.c.domain_id == point.domain.id,
            h.c.branch == point.layout.branch,
            tuple_(h.c.partition, h.c.element_key).in_(list(elements)),
        )
        .order_by(r.c.generation.desc(), h.c.partition, h.c.element_key)
        .limit(limit)
    )
    if floor is not None:
        query = query.where(r.c.generation > floor)
    rows = list((await session.execute(query)).mappings())
    items = [
        ChangeItem(
            revision=bytes(row["revision_id"]).hex(),
            at=timestamp(row["at"]) or "",
            ref=elements[(row["partition"], row["element_key"])],
            change=_change_kind(row["old_hash"], row["new_hash"]),
            attributes=_attribute_names(point.schema, row["partition"], row["provenance"]),
            actor=row["author"],
            origin=str((row["origin"] or {}).get("type", "")),
            # Messages naming something invisible to the caller are left out (design 13.3).
            message="" if point.tells(row["message"]) else row["message"],
        )
        for row in reversed(rows)
    ]
    if point.hidden:
        # A change of nothing but Attributes left out of the caller's schema tells that
        # something invisible changed.
        items = [i for i in items if i.change != "modified" or i.attributes]
    return tuple(items)


async def lineage_items(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    rows: Sequence[Mapping[Any, Any]],
) -> tuple[LineageItem, ...]:
    """Per seed, its Attributes grouped by the provenance entry of their last change."""
    ids = [p for row in rows for p in (row.get("lineage") or {}).values()]
    entries = await ProvenanceRepository(
        session, point.domain.id, writes=runtime.store_settings.writes
    ).get_many(ids)
    items: list[LineageItem] = []
    for row in rows:
        kind = point.schema.kinds.get(row["kind_id"])
        if kind is None:
            continue
        lineage: Mapping[str, str] = row.get("lineage") or {}
        groups: dict[str, list[str]] = {}
        for attribute in ordered_attributes(kind):
            provenance = lineage.get(attribute.id)
            if provenance is not None:
                groups.setdefault(provenance, []).append(attribute.api_name)
        for provenance, names in groups.items():
            entry = entries.get(provenance)
            items.append(
                LineageItem(
                    ref=ref_of(kind.api_name, row["key"]),
                    origin=None if entry is None else entry.origin_type,
                    actor=None if entry is None else entry.actor,
                    at=None if entry is None else timestamp(entry.at),
                    attributes=tuple(names),
                )
            )
    return tuple(items)


# The pack -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackContext:
    """What the route resolved before building: where, for whom, and the cache tag."""

    point: ReadPoint
    etag: str
    since: str | None
    floor: int | None


async def build_pack(
    session: AsyncSession,
    runtime: ReadRuntime,
    context: PackContext,
    request: ContextRequest,
    settings: ContextSettings,
) -> tuple[bytes, str]:
    """The rendered pack and its media type."""
    bounds = effective(request, settings, runtime)
    point = await with_rows(session, runtime, context.point, "Context packs")
    addresses = await seed_addresses(session, runtime, point, request, settings)
    rows = await fetch_rows(session, point, addresses)
    seeds = [record_out(point.schema, row, masking=point.masking) for row in rows]
    around = await surroundings(session, runtime, point, seeds, request, settings, bounds.depth)
    changes = None
    if request.include_recent_changes:
        records = list(addresses)
        for neighbor in around.neighbors:
            kind = point.schema.kind_by_api_name(neighbor.kind)
            if kind is not None:
                records.append((kind, neighbor.ref.partition("/")[2]))
        changes = await recent_changes(
            session,
            point,
            records,
            around.links,
            floor=context.floor,
            limit=settings.recent_changes_max,
        )
    lineage = (
        await lineage_items(session, runtime, point, rows) if request.include_lineage else None
    )
    draft = Draft(
        seeds=tuple(
            SeedItem(
                ref=ref_of(s.kind, s.key),
                kind=s.kind,
                title=s.title,
                values=ordered_values(point.schema.kind_by_api_name(s.kind), s.values),
                updated_revision=s.updated_revision,
            )
            for s in seeds
        ),
        neighbors=around.neighbors,
        links=around.links,
        schema=request.include_schema,
        changes=changes,
        lineage=lineage,
    )
    markdown = request.format == "markdown"
    measure = markdown_measure(point.schema) if markdown else json_measure(point.schema)
    result = fit(
        draft,
        budget_tokens=bounds.budget_tokens,
        chars_per_token=settings.chars_per_token,
        measure=measure,
        order=settings.trim_order,
    )
    budget = budget_json(
        result,
        draft,
        budget_tokens=bounds.budget_tokens,
        chars_per_token=settings.chars_per_token,
        capped=around.capped,
    )
    if markdown:
        # References are resolved when the pack is sent, from the envelope (never by
        # matching the text, where a stored string may look like one).
        envelope = deferred_markdown(
            result.draft,
            lambda draft: pack_markdown(
                markdown_sections(draft, point.schema),
                domain=point.domain.id,
                branch=point.branch,
                revision=point.revision_hex,
                budget=budget,
                since=context.since,
            ),
        )
        return envelope, MARKDOWN
    document: dict[str, Any] = {
        "domain": point.domain.id,
        "branch": point.branch,
        "revision": point.revision_hex,
        "etag": context.etag,
        **json_sections(result.draft, point.schema),
    }
    if context.since is not None:
        document["since"] = context.since
    document["budget"] = budget
    return dumps(document).encode(), JSON
