"""Traversal and neighborhoods (design 8.3).

Traversal runs one set-based query per hop, capped per hop. A neighborhood is one statement
of the store primitive `lorebase_store.traverse.neighborhood` (a recursive walk with fan-out,
Link and node caps). Both read the materialized head; at another Revision they are refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Kind, LinkDirection
from lorebase_core.query import (
    Hop,
    QueryError,
    Target,
    condition_kinds,
    resolve_hop,
    resolve_where,
)
from lorebase_keeper.reading.points import ReadPoint, not_found
from lorebase_keeper.reading.problems import invalid, query_problem, tree_unsupported
from lorebase_keeper.reading.records import compiler_for, get_record, require_kind
from lorebase_keeper.reading.requests import NeighborhoodRequest, TraverseRequest
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.reading.views import (
    Edge,
    NeighborhoodResponse,
    RecordOut,
    TraverseHopOut,
    TraverseResponse,
    link_values,
    record_out,
    record_ref,
    served,
)
from lorebase_store.reads import Direction, hydrate_records
from lorebase_store.repositories.materialized import RecordRow
from lorebase_store.traverse import neighborhood as walk_neighborhood


def _edge(point: ReadPoint, hop: Hop, row: Mapping[Any, Any]) -> Edge:
    return Edge(
        relation=hop.name,
        source=record_ref(point.schema, hop.near_kind, row["near_key"]),
        to=record_ref(point.schema, hop.far.kind_ids[0], row["far_key"], row["title"]),
        values=link_values(hop.relation, row["link_values"]),
    )


async def traverse(
    session: AsyncSession, runtime: ReadRuntime, point: ReadPoint, request: TraverseRequest
) -> TraverseResponse:
    """Follow `request.path` from the start Record; each hop keeps at most its limit."""
    if not point.materialized:
        raise tree_unsupported("Traversal")
    caps = runtime.read.traverse
    if len(request.path) > caps.max_hops:
        raise invalid("/body/path", f"a path has at most {caps.max_hops} steps", "limit_exceeded")
    kind = require_kind(point, request.start.kind)
    start, _ = await get_record(
        session, runtime, point, kind, request.start.key, links=False, lineage=False
    )
    compiler = compiler_for(point, runtime)
    current = Target(kind.api_name, (kind,))
    frontier = [request.start.key]
    hops: list[TraverseHopOut] = []
    for index, step in enumerate(request.path):
        pointer = f"/body/path/{index}"
        hop = resolve_hop(point.schema, current, step.relation)
        if isinstance(hop, str):
            raise invalid(f"{pointer}/relation", hop, "unknown_relation")
        point.scope.require(k.api_name for k in hop.far.kinds)
        limit = step.limit or caps.max_nodes_per_hop
        if limit > caps.max_nodes_per_hop:
            raise invalid(
                f"{pointer}/limit", f"must not exceed {caps.max_nodes_per_hop}", "limit_exceeded"
            )
        try:
            where = (
                None
                if step.where is None
                else resolve_where(
                    step.where, hop.far, point.schema, runtime.read.query_limits(), "/where"
                )
            )
        except QueryError as error:
            raise query_problem(error, pointer) from None
        point.scope.require(condition_kinds(where))
        rows: list[Mapping[Any, Any]] = []
        if frontier:
            query = compiler.neighbors(hop, frontier, where=where, limit=limit + 1)
            rows = list((await session.execute(query)).mappings())
        truncated = len(rows) > limit
        rows = rows[:limit]
        reached: dict[str, RecordOut] = {}
        for row in rows:
            reached.setdefault(row["far_key"], record_out(point.schema, row))
        hops.append(
            TraverseHopOut(
                relation=hop.name,
                records=list(reached.values()),
                edges=[_edge(point, hop, row) for row in rows],
                truncated=truncated,
            )
        )
        frontier = list(reached)
        current = hop.far
    return TraverseResponse(**served(point), start=start, hops=hops)


async def neighborhood(
    session: AsyncSession, runtime: ReadRuntime, point: ReadPoint, request: NeighborhoodRequest
) -> NeighborhoodResponse:
    """Records and Links around the seeds, breadth first, within the depth and the caps.

    One statement of the store primitive `lorebase_store.traverse.neighborhood`; caps are the
    smaller of `read.traverse` and `store.traverse`. Nodes come nearest first; each Link is
    reported once, by its forward name, from its `from` Record to its `to` Record.
    """
    if not point.materialized:
        raise tree_unsupported("Neighborhoods")
    caps, store = runtime.read.traverse, runtime.store_settings.traverse
    max_seeds = min(caps.max_seeds, store.max_seeds)
    if len(request.seeds) > max_seeds:
        raise invalid("/body/seeds", f"at most {max_seeds} seeds", "limit_exceeded")
    max_depth = min(caps.max_depth, store.max_depth)
    if request.depth > max_depth:
        raise invalid("/body/depth", f"must not exceed {max_depth}", "limit_exceeded")
    nodes_cap = min(caps.max_nodes, store.max_nodes)
    edges_cap = min(caps.max_edges, store.max_links)
    max_nodes = request.max_nodes or nodes_cap
    max_edges = request.max_edges or edges_cap
    if max_nodes > nodes_cap:
        raise invalid("/body/max_nodes", f"must not exceed {nodes_cap}", "limit_exceeded")
    if max_edges > edges_cap:
        raise invalid("/body/max_edges", f"must not exceed {edges_cap}", "limit_exceeded")
    follow = _follow(point, _followed(point, request.relations))
    seeds = [(require_kind(point, seed.kind).id, seed.key) for seed in request.seeds]
    found = await hydrate_records(
        session, point.domain.id, point.branch, seeds, runtime=runtime.store
    )
    for kind_id, key in seeds:
        if (kind_id, key) not in found:
            raise not_found(f"Record {point.schema.kinds[kind_id].api_name}/{key}")

    walked = await walk_neighborhood(
        session,
        point.domain.id,
        point.branch,
        point.schema,
        seeds,
        depth=request.depth,
        follow=follow,
        max_nodes=max_nodes,
        max_links=max_edges,
        runtime=runtime.store,
    )
    titles = {(n.record.kind_id, n.record.key): n.record.title for n in walked.records}
    nodes = [record_out(point.schema, _row(n.record)) for n in walked.records]
    edges = []
    for link in walked.links:
        relation = point.schema.relations[link.relation_id]
        edges.append(
            Edge(
                relation=relation.api_name,
                source=record_ref(point.schema, relation.from_kind, link.from_key),
                to=record_ref(
                    point.schema,
                    relation.to_kind,
                    link.to_key,
                    titles.get((relation.to_kind, link.to_key)),
                ),
                values=link_values(relation, link.values),
            )
        )
    return NeighborhoodResponse(
        **served(point), nodes=nodes, edges=edges, truncated=walked.truncated
    )


def _row(record: RecordRow) -> dict[str, Any]:
    """A store Record row as the mapping `record_out` reads."""
    return {
        "kind_id": record.kind_id,
        "key": record.key,
        "title": record.title,
        "values": record.values,
        "updated_revision": record.updated_revision,
        "updated_at": record.updated_at,
    }


def _followed(point: ReadPoint, names: list[str] | None) -> set[tuple[str, LinkDirection]] | None:
    if names is None:
        return None
    wanted: set[tuple[str, LinkDirection]] = set()
    for index, name in enumerate(names):
        found = point.schema.relation_by_api_name(name)
        if found is None:
            raise invalid(f"/body/relations/{index}", f"unknown Relation {name!r}", "unknown")
        relation, direction = found
        wanted.add((relation.id, direction))
    return wanted


def _follow(
    point: ReadPoint, wanted: set[tuple[str, LinkDirection]] | None
) -> list[tuple[str, Direction]]:
    """`(Relation id, direction)` pairs to walk: those requested (all by default) whose far
    Kind the caller may read; Kinds the caller may not read are left out."""
    follow: list[tuple[str, Direction]] = []
    for relation in sorted(point.schema.relations.values(), key=lambda r: r.id):
        for direction in LinkDirection:
            if wanted is not None and (relation.id, direction) not in wanted:
                continue
            far: Kind | None = point.schema.kinds.get(relation.far_endpoint(direction))
            if far is None or not point.scope.allows(far.api_name):
                continue
            forward = direction is LinkDirection.FORWARD
            follow.append((relation.id, Direction.OUT if forward else Direction.IN))
    return follow
