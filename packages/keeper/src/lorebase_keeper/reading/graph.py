"""Traversal and neighborhoods (design 8.3): one set-based query per hop, capped per hop.

Both read the materialized head; at another Revision they are refused.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
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
from lorebase_keeper.reading.compiler import record_columns
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
from lorebase_store.repositories.materialized import MAT_RECORDS


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
    """Records and Links around the seeds, breadth first, within the depth and the caps."""
    if not point.materialized:
        raise tree_unsupported("Neighborhoods")
    caps = runtime.read.traverse
    if len(request.seeds) > caps.max_seeds:
        raise invalid("/body/seeds", f"at most {caps.max_seeds} seeds", "limit_exceeded")
    if request.depth > caps.max_depth:
        raise invalid("/body/depth", f"must not exceed {caps.max_depth}", "limit_exceeded")
    max_nodes = request.max_nodes or caps.max_nodes
    max_edges = request.max_edges or caps.max_edges
    if max_nodes > caps.max_nodes:
        raise invalid("/body/max_nodes", f"must not exceed {caps.max_nodes}", "limit_exceeded")
    if max_edges > caps.max_edges:
        raise invalid("/body/max_edges", f"must not exceed {caps.max_edges}", "limit_exceeded")
    wanted = _followed(point, request.relations)

    nodes: dict[tuple[str, str], RecordOut] = {}
    frontier: dict[str, set[str]] = defaultdict(set)
    seeds: dict[str, list[str]] = defaultdict(list)
    for seed in request.seeds:
        seeds[require_kind(point, seed.kind).id].append(seed.key)
    r = MAT_RECORDS
    for kind_id, keys in seeds.items():
        seed_rows = await session.execute(
            select(*record_columns(r)).where(
                r.c.domain_id == point.domain.id,
                r.c.branch == point.branch,
                r.c.kind_id == kind_id,
                r.c.key.in_(keys),
            )
        )
        found = {row["key"]: row for row in seed_rows.mappings()}
        for key in keys:
            if key not in found:
                raise not_found(f"Record {point.schema.kinds[kind_id].api_name}/{key}")
            nodes.setdefault((kind_id, key), record_out(point.schema, found[key]))
            frontier[kind_id].add(key)

    compiler = compiler_for(point, runtime)
    edges: list[Edge] = []
    seen_edges: set[tuple[str, str, str]] = set()
    truncated = False
    for _ in range(request.depth):
        following: dict[str, set[str]] = defaultdict(set)
        for hop in _hops(point, frontier, wanted):
            remaining = max_edges - len(edges)
            if remaining <= 0:
                truncated = True
                break
            query = compiler.neighbors(hop, sorted(frontier[hop.near_kind]), limit=remaining + 1)
            rows = list((await session.execute(query)).mappings())
            if len(rows) > remaining:
                truncated = True
                rows = rows[:remaining]
            far_kind = hop.far.kind_ids[0]
            for row in rows:
                forward = hop.direction is LinkDirection.FORWARD
                identity = (
                    hop.relation.id,
                    row["near_key"] if forward else row["far_key"],
                    row["far_key"] if forward else row["near_key"],
                )
                if identity in seen_edges:
                    continue
                node = (far_kind, row["far_key"])
                if node not in nodes:
                    if len(nodes) >= max_nodes:
                        truncated = True
                        continue
                    nodes[node] = record_out(point.schema, row)
                    following[far_kind].add(row["far_key"])
                seen_edges.add(identity)
                edges.append(_edge(point, hop, row))
        frontier = following
        if not frontier:
            break
    return NeighborhoodResponse(
        **served(point), nodes=list(nodes.values()), edges=edges, truncated=truncated
    )


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


def _hops(
    point: ReadPoint,
    frontier: Mapping[str, set[str]],
    wanted: set[tuple[str, LinkDirection]] | None,
) -> list[Hop]:
    hops: list[Hop] = []
    for relation in sorted(point.schema.relations.values(), key=lambda r: r.id):
        for direction in LinkDirection:
            near = relation.endpoint(direction)
            if not frontier.get(near):
                continue
            if wanted is not None and (relation.id, direction) not in wanted:
                continue
            far: Kind | None = point.schema.kinds.get(relation.far_endpoint(direction))
            if far is None or not point.scope.allows(far.api_name):
                continue  # Kinds the caller may not read are left out of the neighborhood
            name = (
                relation.api_name
                if direction is LinkDirection.FORWARD
                else relation.inverse_api_name
            )
            hops.append(Hop(name, relation, direction, near, Target(far.api_name, (far,))))
    return hops
