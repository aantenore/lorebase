"""Revision events: what a commit changed, for the change feed and webhooks (design 10.5).

The commit pipeline writes one `revision.committed` outbox event per Revision (branch,
Revision, parents, change counts). `describe` turns Revisions into the event documents the
change feed and webhooks send: the Revision metadata, changes per Kind and Relation (api
names), whether the schema changed, and up to `keys_max` changed Record keys.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

from sqlalchemy import Table, and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Schema
from lorebase_store.commit.pipeline import COMMITTED_EVENT
from lorebase_store.commit.schema import load_compiled
from lorebase_store.materialize.rows import PartitionType, parse_partition
from lorebase_store.models import metadata
from lorebase_store.repositories.materialized import MaterializedRepository
from lorebase_store.repositories.revisions import RevisionRepository, RevisionRow
from lorebase_store.settings import WriteSettings

CHANGESETS: Table = metadata.tables["changesets"]
EVENT_TYPES: tuple[str, ...] = (COMMITTED_EVENT,)
"""Event types of the change feed (Revision events; see `outbox_events` for webhooks)."""


@dataclass(frozen=True, slots=True)
class RevisionChanges:
    """Changed elements of one Revision: counts per partition, some Record keys."""

    partitions: Mapping[str, int] = field(default_factory=dict)
    keys: Mapping[str, Sequence[str]] = field(default_factory=dict)
    """Changed Record keys per Kind partition, in key order, `keys_max` in all."""
    keys_truncated: bool = False


async def revision_changes(
    session: AsyncSession,
    domain_id: str,
    revisions: Collection[bytes],
    *,
    keys_max: int,
    kind_partitions: Collection[str] | None = None,
) -> dict[bytes, RevisionChanges]:
    """Changes of each Revision against its first parent (its stored changeset).

    `kind_partitions` restricts the keys listed (not the counts) to those Kinds.
    """
    if not revisions:
        return {}
    c = CHANGESETS
    scope = and_(c.c.domain_id == domain_id, c.c.revision_id.in_(list(revisions)))
    counts: dict[bytes, dict[str, int]] = defaultdict(dict)
    result = await session.execute(
        select(c.c.revision_id, c.c.partition, func.count())
        .where(scope)
        .group_by(c.c.revision_id, c.c.partition)
    )
    for revision, partition, count in result:
        counts[bytes(revision)][partition] = int(count)
    keys: dict[bytes, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    truncated: set[bytes] = set()
    if keys_max > 0:
        records = c.c.partition.startswith("k:")
        if kind_partitions is not None:
            records = c.c.partition.in_(list(kind_partitions))
        ranked = (
            select(
                c.c.revision_id,
                c.c.partition,
                c.c.element_key,
                func.row_number()
                .over(partition_by=c.c.revision_id, order_by=(c.c.partition, c.c.element_key))
                .label("rank"),
            )
            .where(scope, records)
            .subquery()
        )
        rows = await session.execute(
            select(ranked.c.revision_id, ranked.c.partition, ranked.c.element_key)
            .where(ranked.c.rank <= keys_max + 1)
            .order_by(ranked.c.revision_id, ranked.c.rank)
        )
        listed: dict[bytes, int] = defaultdict(int)
        for revision, partition, key in rows:
            revision = bytes(revision)
            if listed[revision] == keys_max:
                truncated.add(revision)
                continue
            listed[revision] += 1
            keys[revision][partition].append(key)
    return {
        revision: RevisionChanges(
            counts.get(revision, {}),
            {p: list(k) for p, k in keys.get(revision, {}).items()},
            revision in truncated,
        )
        for revision in revisions
    }


def _named(schema: Schema | None, partition: str) -> tuple[PartitionType, str]:
    parsed = parse_partition(partition)
    if schema is not None:
        if parsed.type is PartitionType.KIND and parsed.element_id in schema.kinds:
            return parsed.type, schema.kinds[parsed.element_id].api_name
        if parsed.type is PartitionType.RELATION and parsed.element_id in schema.relations:
            return parsed.type, schema.relations[parsed.element_id].api_name
    return parsed.type, parsed.element_id


def revision_event(
    revision: RevisionRow,
    changes: RevisionChanges,
    *,
    domain_id: str,
    branch: str,
    schema: Schema | None,
    sequence: int | None = None,
) -> dict[str, Any]:
    """The JSON document of a `revision.committed` event."""
    kinds: dict[str, int] = {}
    relations: dict[str, int] = {}
    schema_changed = False
    for partition, count in sorted(changes.partitions.items()):
        element, name = _named(schema, partition)
        if element is PartitionType.KIND:
            kinds[name] = kinds.get(name, 0) + count
        elif element is PartitionType.RELATION:
            relations[name] = relations.get(name, 0) + count
        else:
            schema_changed = True
    keys = {_named(schema, p)[1]: list(k) for p, k in sorted(changes.keys.items())}
    document: dict[str, Any] = {}
    if sequence is not None:
        document["sequence"] = sequence
    document.update(
        {
            "type": COMMITTED_EVENT,
            "domain": domain_id,
            "branch": branch,
            "revision": revision.id.hex(),
            "parents": [p.hex() for p in revision.parents],
            "generation": revision.generation,
            "author": revision.author,
        }
    )
    if revision.on_behalf_of is not None:
        document["on_behalf_of"] = revision.on_behalf_of
    document.update(
        {
            "message": revision.message,
            "origin": revision.origin,
            "stats": revision.stats,
            "created_at": revision.created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "kinds": kinds,
            "relations": relations,
            "schema_changed": schema_changed,
            "keys": keys,
            "keys_truncated": changes.keys_truncated,
        }
    )
    return document


@dataclass(frozen=True, slots=True)
class EventRef:
    """A Revision committed on a branch, with its outbox sequence when known."""

    revision: bytes
    branch: str
    sequence: int | None = None


async def describe(
    session: AsyncSession,
    domain_id: str,
    refs: Sequence[EventRef],
    *,
    schemas: Mapping[str, Schema | None],
    keys_max: int,
    kind_partitions: Collection[str] | None = None,
) -> list[dict[str, Any]]:
    """Event documents of `refs`, in their order; Revisions that no longer exist are skipped.

    `schemas` maps each branch to the schema naming its Kinds and Relations.
    """
    repository = RevisionRepository(session, domain_id)
    rows: dict[bytes, RevisionRow] = {}
    for ref in refs:
        if ref.revision not in rows:
            found = await repository.get(ref.revision)
            if found is not None:
                rows[ref.revision] = found
    changes = await revision_changes(
        session, domain_id, list(rows), keys_max=keys_max, kind_partitions=kind_partitions
    )
    return [
        revision_event(
            rows[ref.revision],
            changes[ref.revision],
            domain_id=domain_id,
            branch=ref.branch,
            schema=schemas.get(ref.branch),
            sequence=ref.sequence,
        )
        for ref in refs
        if ref.revision in rows
    ]


async def branch_schema(
    session: AsyncSession, domain_id: str, branch: str, *, writes: WriteSettings
) -> Schema | None:
    """Compiled schema of a branch head (none when the branch is not materialized)."""
    row = await MaterializedRepository(session, domain_id, branch, writes=writes).schema()
    return None if row is None else load_compiled(row.compiled)


_FIRST_PARENTS = text(
    """
    WITH RECURSIVE chain(id, generation, step) AS (
        SELECT r.id, r.generation, 0 FROM revisions r
        WHERE r.domain_id = :domain AND r.id = :head
      UNION ALL
        SELECT r.id, r.generation, chain.step + 1
        FROM chain
        JOIN revision_parents p
          ON p.domain_id = :domain AND p.revision_id = chain.id AND p.position = 0
        JOIN revisions r ON r.domain_id = :domain AND r.id = p.parent_id
        WHERE chain.generation > :floor AND chain.step < :max_steps
    )
    SELECT id, generation FROM chain ORDER BY step
    """
)


async def first_parents(
    session: AsyncSession, domain_id: str, head: bytes, *, floor: int, max_steps: int
) -> list[tuple[bytes, int]]:
    """`head` and its first-parent ancestors, newest first, down to the first Revision at or
    below generation `floor` (included) or `max_steps` steps back."""
    result = await session.execute(
        _FIRST_PARENTS,
        {"domain": domain_id, "head": head, "floor": floor, "max_steps": max_steps},
    )
    return [(bytes(row.id), int(row.generation)) for row in result]


def matches(document: Mapping[str, Any], kinds: Iterable[str] | None) -> bool:
    """Whether an event touches one of `kinds` (api names); every event when `kinds` is None."""
    if kinds is None:
        return True
    return not set(document["kinds"]).isdisjoint(kinds)
