"""Record and Link reads: get, list and query, Links of a Record or a Relation.

Reads at the branch head use the materialized rows and the query compiler; reads at another
Revision go through `TreeReader` for key lookups, listings of a Kind or a Trait ordered by key,
and Links (path Derivations composed from the Links of their steps there), and through a
snapshot of the Revision (`reading.snapshots`) for everything else.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, and_, func, literal, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Kind, LinkDirection, PathRelation, Relation
from lorebase_core.model.paths import walk
from lorebase_core.model.relation import RelationBacking
from lorebase_core.query import (
    KEY_FIELD,
    Hop,
    IncludePlan,
    Query,
    QueryError,
    QueryPlan,
    plan_kinds,
    resolve_hop,
    resolve_query,
    target_of_kind,
)
from lorebase_core.tree import kind_partition, relation_partition
from lorebase_keeper.reading.compiler import (
    Compiler,
    SortKey,
    after,
    cursor_value,
    from_cursor_value,
    json_text,
    order_clauses,
    record_columns,
)
from lorebase_keeper.reading.cursors import invalid_cursor, issue, position_of
from lorebase_keeper.reading.points import ReadPoint, not_found
from lorebase_keeper.reading.problems import query_problem
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.reading.sensitivity import require_comparable_plan
from lorebase_keeper.reading.snapshots import with_rows
from lorebase_keeper.reading.tree import TreeReader
from lorebase_keeper.reading.views import (
    AggregateCount,
    AggregateOut,
    LineageOut,
    LinkCountOut,
    LinkOut,
    RecordOut,
    kind_name,
    link_values,
    record_out,
    record_ref,
    relation_name,
)
from lorebase_keeper.tracing.explain import explain
from lorebase_store.indexes import plan_indexes
from lorebase_store.materialize.rows import record_title, split_link_key
from lorebase_store.reads import hydrate_records, links_to_record
from lorebase_store.repositories import ProvenanceRepository


@dataclass(frozen=True, slots=True)
class FetchedRecord:
    """A Record row (materialized columns, or tree content with the same keys)."""

    row: Mapping[Any, Any]
    object_hash: bytes


@dataclass(frozen=True, slots=True)
class QueryPage:
    items: list[RecordOut]
    next_cursor: str | None
    aggregate: AggregateOut | None = None


@dataclass(frozen=True, slots=True)
class LinkPageResult:
    items: list[LinkOut]
    next_cursor: str | None


def compiler_for(point: ReadPoint, runtime: ReadRuntime) -> Compiler:
    return Compiler(
        point.domain.id,
        point.layout.branch,
        text_search_config=runtime.store_settings.materialize.text_search_config,
        indexes=plan_indexes(point.schema).by_attribute(),
        layout=point.layout,
        path_fanout=runtime.store_settings.traverse.path_fanout,
    )


def require_kind(point: ReadPoint, api_name: str) -> Kind:
    """The Kind named `api_name`: 403 when the caller may not read it, 404 when unknown."""
    point.scope.require([api_name])
    kind = point.schema.kind_by_api_name(api_name)
    if kind is None:
        raise not_found(f"Kind {api_name}")
    return kind


# Single Record --------------------------------------------------------------------------------


async def fetch_record(
    session: AsyncSession, runtime: ReadRuntime, point: ReadPoint, kind: Kind, key: str
) -> FetchedRecord:
    """The Record `(kind, key)` at the read point; 404 when absent."""
    if point.materialized:
        r = point.layout.records()
        found = (
            (
                await session.execute(
                    select(*record_columns(r)).where(
                        r.c.domain_id == point.domain.id,
                        r.c.branch == point.layout.branch,
                        r.c.kind_id == kind.id,
                        r.c.key == key,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if found is None:
            raise not_found(f"Record {kind.api_name}/{key}")
        return FetchedRecord(found, bytes(found["object_hash"]))
    reader = TreeReader(session, runtime, point.domain.id, point.revision.root)
    digest = await reader.get(kind_partition(kind.id), key)
    if digest is None:
        raise not_found(f"Record {kind.api_name}/{key}")
    body = (await reader.bodies([digest]))[digest]
    return FetchedRecord(tree_record_row(kind, key, body), digest)


def tree_record_row(kind: Kind, key: str, body: Mapping[str, Any]) -> dict[str, Any]:
    values = dict(body.get("values", {}))
    return {
        "kind_id": kind.id,
        "key": key,
        "values": values,
        "title": record_title(kind, values),
    }


async def get_record(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    kind: Kind,
    key: str,
    *,
    links: bool,
    lineage: bool,
) -> tuple[RecordOut, FetchedRecord]:
    """A Record with, on request, its Link counts and the lineage of its values."""
    if links or lineage:
        point = await with_rows(session, runtime, point, "The links and lineage summaries")
    fetched = await fetch_record(session, runtime, point, kind, key)
    record = record_out(point.schema, fetched.row, masking=point.masking)
    if links:
        record.links = await link_counts(session, runtime, point, kind, fetched.row)
    if lineage:
        record.lineage = await lineage_summary(session, runtime, point, kind, fetched.row)
    return record, fetched


async def link_counts(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    kind: Kind,
    row: Mapping[Any, Any],
) -> list[LinkCountOut]:
    """Number of Links per Relation and direction touching the Record, path Derivations
    included (a path only when the caller may read every Kind it crosses)."""
    key = row["key"]
    counts: list[LinkCountOut] = []
    explicit: dict[LinkDirection, list[Relation]] = defaultdict(list)
    for relation in sorted(point.schema.relations_of(kind.id), key=lambda r: r.api_name):
        for direction in LinkDirection:
            if relation.endpoint(direction) != kind.id:
                continue
            far = point.schema.kinds.get(relation.far_endpoint(direction))
            if far is None or not point.scope.allows(far.api_name):
                continue  # Links to Kinds the caller may not read are not counted
            if relation.backing is RelationBacking.FOREIGN_KEY:
                count = await _foreign_key_count(session, point, relation, direction, row)
                counts.append(
                    LinkCountOut(
                        relation=relation_name(relation, direction),
                        direction=direction,
                        count=count,
                    )
                )
            else:
                explicit[direction].append(relation)
    links = point.layout.links()
    for direction, relations in explicit.items():
        near = links.c.from_key if direction is LinkDirection.FORWARD else links.c.to_key
        result = await session.execute(
            select(links.c.relation_id, func.count())
            .where(
                links.c.domain_id == point.domain.id,
                links.c.branch == point.layout.branch,
                links.c.relation_id.in_([r.id for r in relations]),
                near == key,
            )
            .group_by(links.c.relation_id)
        )
        found = {relation_id: int(count) for relation_id, count in result}
        counts.extend(
            LinkCountOut(
                relation=relation_name(r, direction), direction=direction, count=found.get(r.id, 0)
            )
            for r in relations
        )
    counts.extend(await _path_counts(session, runtime, point, kind, key))
    return sorted(counts, key=lambda c: (c.relation, c.direction))


async def _path_counts(
    session: AsyncSession, runtime: ReadRuntime, point: ReadPoint, kind: Kind, key: str
) -> list[LinkCountOut]:
    """Link counts of the path Derivations starting or ending at the Record's Kind, stored
    or composed like their Links (`Compiler.link_source`)."""
    target = target_of_kind(point.schema, kind.api_name)
    assert target is not None
    counts: list[LinkCountOut] = []
    compiler: Compiler | None = None
    for path in sorted(point.schema.path_relations().values(), key=lambda p: p.id):
        for direction in LinkDirection:
            name = relation_name(path.relation, direction)
            if not name or path.relation.endpoint(direction) != kind.id:
                continue
            hop = resolve_hop(point.schema, target, name)
            if isinstance(hop, str) or not all(point.scope.allows(k) for k in hop.kinds_read):
                continue  # paths crossing Kinds the caller may not read are not counted
            compiler = compiler or compiler_for(point, runtime)
            source = compiler.link_source(hop)
            count = await session.scalar(
                select(func.count())
                .select_from(source.rows)
                .where(source.scope, source.near == literal(key))
            )
            counts.append(LinkCountOut(relation=name, direction=direction, count=int(count or 0)))
    return counts


async def _foreign_key_count(
    session: AsyncSession,
    point: ReadPoint,
    relation: Relation,
    direction: LinkDirection,
    row: Mapping[Any, Any],
) -> int:
    attribute = relation.foreign_key_attribute
    assert attribute is not None
    if direction is LinkDirection.FORWARD:
        return 1 if attribute in (row.get("values") or {}) else 0
    r = point.layout.records()
    count = await session.scalar(
        select(func.count()).where(
            r.c.domain_id == point.domain.id,
            r.c.branch == point.layout.branch,
            r.c.kind_id == relation.from_kind,
            json_text(r.c["values"], attribute) == row["key"],
        )
    )
    return int(count or 0)


async def lineage_summary(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    kind: Kind,
    row: Mapping[Any, Any],
) -> dict[str, LineageOut]:
    """Per Attribute api name, the provenance entry of its last change."""
    lineage: Mapping[str, str] = row.get("lineage") or {}
    entries = await ProvenanceRepository(
        session, point.domain.id, writes=runtime.store_settings.writes
    ).get_many(lineage.values())
    summary: dict[str, LineageOut] = {}
    for attribute_id, provenance_id in lineage.items():
        attribute = kind.attributes.get(attribute_id)
        if attribute is None:
            continue
        entry = entries.get(provenance_id)
        summary[attribute.api_name] = LineageOut(
            provenance_id=provenance_id,
            origin_type=None if entry is None else entry.origin_type,
            actor=None if entry is None else entry.actor,
            at=None if entry is None else entry.at,
        )
    return summary


# Lists and queries ------------------------------------------------------------------------


def plan_query(query: Query, point: ReadPoint, runtime: ReadRuntime, prefix: str) -> QueryPlan:
    """The resolved query, once every Kind it reads is one the caller may read."""
    if query.kind is not None:
        point.scope.require([query.kind])
    try:
        plan = resolve_query(query, point.schema, runtime.read.query_limits())
    except QueryError as error:
        raise query_problem(error, prefix) from None
    point.scope.require(plan_kinds(plan))
    require_comparable_plan(point.schema, point.masking, plan, prefix)
    return plan


def _key_ordered(plan: QueryPlan) -> bool:
    order = plan.order
    return not order or (
        len(order) == 1 and order[0].field.name == KEY_FIELD and not order[0].descending
    )


async def run_query(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    plan: QueryPlan,
    *,
    shape: str,
    cursor_pointer: str,
) -> QueryPage:
    """A page of the Records matching a resolved query (plus includes and aggregate)."""
    if not point.materialized:
        if plan.where is None and not plan.include and not plan.aggregate and _key_ordered(plan):
            return await _tree_list(session, runtime, point, plan, shape, cursor_pointer)
        what = "Filtering, ordering, includes and aggregates"
        point = await with_rows(session, runtime, point, what)
    compiler = compiler_for(point, runtime)
    r = compiler.records("r")
    target = plan.target
    keys = compiler.sort_keys(r, plan.order, target)
    items: list[RecordOut] = []
    next_cursor = None
    if plan.limit > 0:
        query: Select[Any] = select(
            *record_columns(r), *(k.expression.label(f"s{i}") for i, k in enumerate(keys))
        ).where(compiler.record_scope(r, target))
        if plan.where is not None:
            query = query.where(compiler.condition(r, plan.where, target))
        position = position_of(plan.cursor, shape, cursor_pointer)
        if position is not None:
            query = query.where(after(keys, _decode_position(keys, position, cursor_pointer)))
        query = query.order_by(*order_clauses(keys)).limit(plan.limit + 1)
        await explain(session, query, point.domain.id, "query")
        rows = list((await session.execute(query)).mappings())
        if len(rows) > plan.limit:
            rows = rows[: plan.limit]
            last = rows[-1]
            next_cursor = issue(shape, [cursor_value(last[f"s{i}"]) for i in range(len(keys))])
        items = [
            record_out(point.schema, row, masking=point.masking, select=plan.select) for row in rows
        ]
        for include in plan.include:
            await _include(session, compiler, point, include, rows, items)
    aggregate = None
    if plan.aggregate is not None:
        aggregate = await _aggregate(session, runtime, compiler, point, plan)
    return QueryPage(items, next_cursor, aggregate)


def _decode_position(keys: Sequence[SortKey], position: list[Any], pointer: str) -> list[Any]:
    if len(position) != len(keys):
        raise invalid_cursor(pointer)
    try:
        return [from_cursor_value(v, k.type) for k, v in zip(keys, position, strict=True)]
    except ValueError:
        raise invalid_cursor(pointer) from None


async def _include(
    session: AsyncSession,
    compiler: Compiler,
    point: ReadPoint,
    include: IncludePlan,
    rows: Sequence[Mapping[Any, Any]],
    items: list[RecordOut],
) -> None:
    hop = include.hop
    near = [row["key"] for row in rows if row["kind_id"] == hop.near_kind]
    found: dict[str, list[RecordOut]] = defaultdict(list)
    if near:
        query = compiler.neighbors(hop, near, where=include.where, per_near=include.limit)
        for row in (await session.execute(query)).mappings():
            found[row["near_key"]].append(
                record_out(point.schema, row, masking=point.masking, select=include.select)
            )
    for row, item in zip(rows, items, strict=True):
        if row["kind_id"] != hop.near_kind:
            continue
        item.included = item.included or {}
        item.included[hop.name] = found.get(row["key"], [])


async def _aggregate(
    session: AsyncSession,
    runtime: ReadRuntime,
    compiler: Compiler,
    point: ReadPoint,
    plan: QueryPlan,
) -> AggregateOut:
    r = compiler.records("r")
    target = plan.target
    assert plan.aggregate is not None
    group = plan.aggregate.group_by
    columns: list[Any] = [r.c.kind_id]
    if group is not None:
        columns.append(compiler.field_json(r, group).label("value"))
    query = select(*columns, func.count().label("n")).where(compiler.record_scope(r, target))
    if plan.where is not None:
        query = query.where(compiler.condition(r, plan.where, target))
    cap = runtime.read.query.max_aggregate_groups
    # Grouped by output position: the group expression binds its Attribute id as a parameter,
    # so repeating it in GROUP BY would not match the selected column.
    groups: list[Any] = [literal_column(str(i + 1)) for i in range(len(columns))]
    query = query.group_by(*groups).order_by(*groups).limit(cap + 1)
    rows = list((await session.execute(query)).mappings())
    return AggregateOut(
        counts=[
            AggregateCount(
                kind=kind_name(point.schema, row["kind_id"]),
                value=row["value"] if group is not None else None,
                count=int(row["n"]),
            )
            for row in rows[:cap]
        ],
        truncated=len(rows) > cap,
    )


async def _tree_list(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    plan: QueryPlan,
    shape: str,
    cursor_pointer: str,
) -> QueryPage:
    """A page of the target's Records at a Revision, in the order of the head's keyset
    (`Compiler.sort_keys`): by key for one Kind; for a Trait by Kind id and key, or by key
    and Kind id when ordered by key. Cursors carry the same positions as at the head."""
    kinds = plan.target.kinds
    by_key = len(kinds) == 1 or bool(plan.order)
    reader = TreeReader(session, runtime, point.domain.id, point.revision.root)

    def position(kind: Kind, key: str) -> list[str]:
        if len(kinds) == 1:
            return [key]
        return [key, kind.id] if by_key else [kind.id, key]

    entries = [
        (position(kind, entry.key), kind, entry)
        for kind in kinds
        for entry in await reader.entries(kind_partition(kind.id))
    ]
    entries.sort(key=lambda item: item[0])
    after_position = position_of(plan.cursor, shape, cursor_pointer)
    if after_position is not None:
        width = 1 if len(kinds) == 1 else 2
        if len(after_position) != width or not all(isinstance(p, str) for p in after_position):
            raise invalid_cursor(cursor_pointer)
        entries = [item for item in entries if item[0] > after_position]
    page = entries[: plan.limit]
    bodies = await reader.bodies(entry.object_hash for _, _, entry in page)
    items = [
        record_out(
            point.schema,
            tree_record_row(kind, entry.key, bodies[entry.object_hash]),
            masking=point.masking,
            select=plan.select,
        )
        for _, kind, entry in page
    ]
    more = len(entries) > plan.limit and plan.limit > 0
    return QueryPage(items, issue(shape, page[-1][0]) if more else None)


# Links ----------------------------------------------------------------------------------


def require_hop(point: ReadPoint, kind: Kind, name: str) -> Hop:
    target = target_of_kind(point.schema, kind.api_name)
    assert target is not None
    hop = resolve_hop(point.schema, target, name)
    if isinstance(hop, str):
        raise not_found(f"Relation {name} of Kind {kind.api_name}")
    point.scope.require([kind.api_name, *sorted(hop.kinds_read)])
    return hop


def _link_out(
    point: ReadPoint, hop: Hop, near: str, far: str, values: Any, title: str | None = None
) -> LinkOut:
    forward = hop.direction is LinkDirection.FORWARD
    return LinkOut(
        relation=relation_name(hop.relation, hop.direction),
        direction=hop.direction,
        from_key=near if forward else far,
        to_key=far if forward else near,
        values=link_values(point.schema, hop.relation, values, point.masking),
        target=record_ref(point.schema, hop.far.kind_ids[0], far, title, point.masking),
    )


async def record_links(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    kind: Kind,
    key: str,
    relation: str,
    *,
    limit: int,
    cursor: str | None,
    shape: str,
) -> LinkPageResult:
    """Links of one Record along a Relation (forward or inverse name), by far key."""
    hop = require_hop(point, kind, relation)
    await fetch_record(session, runtime, point, kind, key)
    position = position_of(cursor, shape, "/query/cursor")
    if position is not None and (len(position) != 1 or not isinstance(position[0], str)):
        raise invalid_cursor("/query/cursor")
    after_key = None if position is None else position[0]
    if not point.materialized:
        pairs = await _tree_links(session, runtime, point, hop, key)
        pairs = [p for p in pairs if after_key is None or p[0] > after_key]
        page = pairs[:limit]
        items = [_link_out(point, hop, key, far, values) for far, values in page]
        more = len(pairs) > limit
        return LinkPageResult(items, issue(shape, [page[-1][0]]) if more and page else None)
    composed = hop.path is not None and not hop.path.materialized
    if hop.direction is LinkDirection.INVERSE and not composed:
        return await _inbound_links(session, runtime, point, hop, key, limit, after_key, shape)
    compiler = compiler_for(point, runtime)
    source = compiler.link_source(hop)
    far = point.layout.records("far")
    query = (
        select(source.far.label("far_key"), source.values.label("link_values"), far.c.title)
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
        .limit(limit + 1)
    )
    if after_key is not None:
        query = query.where(source.far > literal(after_key))
    rows = list((await session.execute(query)).mappings())
    more = len(rows) > limit
    rows = rows[:limit]
    items = [
        _link_out(point, hop, key, row["far_key"], row["link_values"], row["title"]) for row in rows
    ]
    return LinkPageResult(items, issue(shape, [rows[-1]["far_key"]]) if more else None)


async def _inbound_links(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    hop: Hop,
    key: str,
    limit: int,
    after_key: str | None,
    shape: str,
) -> LinkPageResult:
    """Links pointing at the Record along an inverse name, through the store primitive
    (`links_to_record`: the reverse Link index, or the from Kind's per-Attribute index for
    foreign-key Relations, with index plans under row-level security)."""
    domain_id, branch, store = point.domain.id, point.layout, runtime.store
    links = await links_to_record(
        session, domain_id, branch, hop.relation, key, limit=limit, after=after_key, runtime=store
    )
    more = len(links) == limit and bool(
        await links_to_record(
            session,
            domain_id,
            branch,
            hop.relation,
            key,
            limit=1,
            after=links[-1].from_key,
            runtime=store,
        )
    )
    far_kind = hop.far.kind_ids[0]
    found = await hydrate_records(
        session, domain_id, branch, [(far_kind, link.from_key) for link in links], runtime=store
    )
    items = []
    for link in links:
        record = found.get((far_kind, link.from_key))
        title = None if record is None else record.title
        items.append(_link_out(point, hop, key, link.from_key, link.values, title))
    return LinkPageResult(items, issue(shape, [links[-1].from_key]) if more else None)


async def _tree_links(
    session: AsyncSession, runtime: ReadRuntime, point: ReadPoint, hop: Hop, key: str
) -> list[tuple[str, Mapping[str, Any]]]:
    """`(far_key, link values)` of a Record's Links at a Revision, sorted by far key."""
    reader = TreeReader(session, runtime, point.domain.id, point.revision.root)
    if hop.path is not None:
        pairs = await _tree_path_pairs(reader, runtime, hop.path)
        forward = hop.direction is LinkDirection.FORWARD
        return sorted((t if forward else f, {}) for f, t in pairs if (f if forward else t) == key)
    relation = hop.relation
    forward = hop.direction is LinkDirection.FORWARD
    if hop.foreign_key:
        attribute = relation.foreign_key_attribute
        assert attribute is not None
        if forward:
            digest = await reader.get(kind_partition(relation.from_kind), key)
            body = {} if digest is None else (await reader.bodies([digest]))[digest]
            target = body.get("values", {}).get(attribute)
            return [] if target is None else [(str(target), {})]
        entries = await reader.entries(kind_partition(relation.from_kind))
        bodies = await reader.bodies(e.object_hash for e in entries)
        return [
            (e.key, {})
            for e in entries
            if bodies[e.object_hash].get("values", {}).get(attribute) == key
        ]
    entries = await reader.entries(relation_partition(relation.id))
    matched: list[tuple[str, bytes]] = []
    for entry in entries:
        from_key, to_key = split_link_key(entry.key)
        near, far = (from_key, to_key) if forward else (to_key, from_key)
        if near == key:
            matched.append((far, entry.object_hash))
    bodies = await reader.bodies(digest for _, digest in matched)
    return sorted((far, bodies[d].get("values", {})) for far, d in matched)


async def relation_links(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    relation_name_: str,
    *,
    from_key: str | None,
    to_key: str | None,
    limit: int,
    cursor: str | None,
    shape: str,
) -> LinkPageResult:
    """Links of a Relation ordered by `(from_key, to_key)`, optionally from or to one key."""
    found = point.schema.relation_by_api_name(relation_name_)
    along = point.schema.path_by_api_name(relation_name_)
    if found is None and along is not None:
        found = (along[0].relation, along[1])
    if found is None:
        raise not_found(f"Relation {relation_name_}")
    relation, _ = found
    from_kind = point.schema.kinds.get(relation.from_kind)
    if from_kind is None:
        raise not_found(f"Relation {relation_name_}")
    hop = require_hop(point, from_kind, relation.api_name)
    position = position_of(cursor, shape, "/query/cursor")
    if position is not None and (
        len(position) != 2 or not all(isinstance(p, str) for p in position)
    ):
        raise invalid_cursor("/query/cursor")
    if not point.materialized:
        pairs = await _tree_relation_links(session, runtime, point, hop)
        pairs = [
            p
            for p in pairs
            if (from_key is None or p[0] == from_key)
            and (to_key is None or p[1] == to_key)
            and (position is None or (p[0], p[1]) > (position[0], position[1]))
        ]
        page = pairs[:limit]
        items = [_link_out(point, hop, f, t, v) for f, t, v in page]
        more = len(pairs) > limit
        return LinkPageResult(items, issue(shape, list(page[-1][:2])) if more and page else None)
    compiler = compiler_for(point, runtime)
    source = compiler.link_source(hop)
    query = (
        select(
            source.near.label("from_key"),
            source.far.label("to_key"),
            source.values.label("link_values"),
        )
        .select_from(source.rows)
        .where(source.scope)
        .order_by(source.near, source.far)
        .limit(limit + 1)
    )
    if from_key is not None:
        query = query.where(source.near == literal(from_key))
    if to_key is not None:
        query = query.where(source.far == literal(to_key))
    if position is not None:
        p_from, p_to = literal(position[0]), literal(position[1])
        query = query.where((source.near > p_from) | and_(source.near == p_from, source.far > p_to))
    rows = list((await session.execute(query)).mappings())
    more = len(rows) > limit
    rows = rows[:limit]
    items = [_link_out(point, hop, r["from_key"], r["to_key"], r["link_values"]) for r in rows]
    next_cursor = issue(shape, [rows[-1]["from_key"], rows[-1]["to_key"]]) if more else None
    return LinkPageResult(items, next_cursor)


async def _tree_relation_links(
    session: AsyncSession, runtime: ReadRuntime, point: ReadPoint, hop: Hop
) -> list[tuple[str, str, Mapping[str, Any]]]:
    reader = TreeReader(session, runtime, point.domain.id, point.revision.root)
    if hop.path is not None:
        return [(f, t, {}) for f, t in sorted(await _tree_path_pairs(reader, runtime, hop.path))]
    return await _tree_pairs(reader, hop.relation)


async def _tree_pairs(
    reader: TreeReader, relation: Relation
) -> list[tuple[str, str, Mapping[str, Any]]]:
    """Every Link of a Relation at a Revision as `(from_key, to_key, values)`, sorted."""
    if relation.backing is RelationBacking.FOREIGN_KEY:
        attribute = relation.foreign_key_attribute
        assert attribute is not None
        entries = await reader.entries(kind_partition(relation.from_kind))
        bodies = await reader.bodies(e.object_hash for e in entries)
        pairs: list[tuple[str, str, Mapping[str, Any]]] = []
        for e in entries:
            target = bodies[e.object_hash].get("values", {}).get(attribute)
            if target is not None:
                pairs.append((e.key, str(target), {}))
        return sorted(pairs, key=lambda p: (p[0], p[1]))
    entries = await reader.entries(relation_partition(relation.id))
    bodies = await reader.bodies(e.object_hash for e in entries)
    out = [(*split_link_key(e.key), bodies[e.object_hash].get("values", {})) for e in entries]
    return sorted(out, key=lambda p: (p[0], p[1]))


async def _tree_path_pairs(
    reader: TreeReader, runtime: ReadRuntime, path: PathRelation
) -> set[tuple[str, str]]:
    """Every Link of a path Derivation at a Revision, composed from the Links of its steps
    there (`lorebase_core.model.paths`; materialized or not, the tree holds no path Links)."""
    maps: list[dict[str, list[str]]] = []
    for step in path.steps:
        forward = step.direction is LinkDirection.FORWARD
        by_near: dict[str, list[str]] = defaultdict(list)
        for from_key, to_key, _ in await _tree_pairs(reader, step.relation):
            near, far = (from_key, to_key) if forward else (to_key, from_key)
            by_near[near].append(far)
        maps.append(by_near)
    steps = [
        (lambda keys, found=found: {k: found[k] for k in keys if k in found}) for found in maps
    ]
    reached = walk(sorted(maps[0]), steps, runtime.store_settings.traverse.path_fanout)
    return {(start, far) for start, fars in reached.items() for far in fars}
