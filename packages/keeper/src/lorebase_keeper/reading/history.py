"""Revisions, the log of a branch and per-partition counts (design 6.3, 6.4, 11.1).

The log walks the first-parent chain of the branch head (`all=true`: every ancestor) with a
recursive query, newest first by generation; pages continue with a `(generation, id)` keyset.
The walk covers the whole history on every page, which is fine for M1 history sizes.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import LargeBinary, Table, cast, literal, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Schema
from lorebase_keeper.reading.compiler import json_text
from lorebase_keeper.reading.diff import partition_name
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_store.materialize.rows import PartitionType
from lorebase_store.models import metadata
from lorebase_store.nodestore import NodeRepository
from lorebase_store.repositories import RevisionRow

REVISIONS: Table = metadata.tables["revisions"]
REVISION_PARENTS: Table = metadata.tables["revision_parents"]


class RevisionOut(BaseModel):
    id: str
    parents: list[str]
    root: str
    generation: int
    author: str
    on_behalf_of: str | None = None
    message: str
    origin: dict[str, Any]
    stats: dict[str, Any]
    created_at: datetime


class PartitionCount(BaseModel):
    partition: str
    element: PartitionType
    name: str
    count: int


def revision_out(row: RevisionRow) -> RevisionOut:
    return RevisionOut(
        id=row.id.hex(),
        parents=[p.hex() for p in row.parents],
        root=row.root.hex(),
        generation=row.generation,
        author=row.author,
        on_behalf_of=row.on_behalf_of,
        message=row.message,
        origin=row.origin,
        stats=row.stats,
        created_at=row.created_at,
    )


async def log(
    session: AsyncSession,
    domain_id: str,
    head: bytes,
    *,
    first_parent: bool,
    origin_type: str | None,
    author: str | None,
    limit: int,
    after: tuple[int, bytes] | None,
) -> tuple[list[RevisionOut], bool]:
    """A page of the history of `head`, newest first; the flag tells whether more follow."""
    rp = REVISION_PARENTS
    chain = select(cast(literal(head, LargeBinary), LargeBinary).label("id")).cte(
        "chain", recursive=True
    )
    step = select(rp.c.parent_id).where(rp.c.domain_id == domain_id, rp.c.revision_id == chain.c.id)
    if first_parent:
        step = step.where(rp.c.position == 0)
    chain = chain.union(step)
    r = REVISIONS
    query = (
        select(
            r.c.id,
            r.c.root,
            r.c.generation,
            r.c.author,
            r.c.on_behalf_of,
            r.c.message,
            r.c.origin,
            r.c.stats,
            r.c.created_at,
        )
        .join(chain, r.c.id == chain.c.id)
        .where(r.c.domain_id == domain_id)
        .order_by(r.c.generation.desc(), r.c.id.desc())
        .limit(limit + 1)
    )
    if origin_type is not None:
        query = query.where(json_text(r.c.origin, "type") == origin_type)
    if author is not None:
        query = query.where(or_(r.c.author == author, r.c.on_behalf_of == author))
    if after is not None:
        generation, last = after
        query = query.where(
            tuple_(r.c.generation, r.c.id) < tuple_(literal(generation), literal(last, LargeBinary))
        )
    rows = list((await session.execute(query)).mappings())
    more = len(rows) > limit
    rows = rows[:limit]
    parents: dict[bytes, list[bytes]] = defaultdict(list)
    if rows:
        result = await session.execute(
            select(rp.c.revision_id, rp.c.parent_id)
            .where(rp.c.domain_id == domain_id, rp.c.revision_id.in_([row["id"] for row in rows]))
            .order_by(rp.c.revision_id, rp.c.position)
        )
        for revision, parent in result:
            parents[revision].append(parent)
    items = [revision_out(RevisionRow(parents=tuple(parents[row["id"]]), **row)) for row in rows]
    return items, more


async def partition_counts(
    session: AsyncSession,
    runtime: ReadRuntime,
    domain_id: str,
    revision: RevisionRow,
    schema: Schema,
) -> list[PartitionCount]:
    """Entries per partition at a Revision, read from the tree root in O(1)."""
    settings = runtime.store_settings
    nodes = NodeRepository(
        session,
        domain_id,
        writes=settings.writes,
        cache=runtime.store.node_cache,
        batch=settings.nodes.prefetch_batch,
    )
    tree = await nodes.load_tree(revision.root)
    counts = []
    for partition, ref in sorted(tree.partitions.items()):
        element, name = partition_name(partition, [schema])
        counts.append(
            PartitionCount(partition=partition, element=element, name=name, count=ref.count)
        )
    return counts
