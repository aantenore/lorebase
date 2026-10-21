"""Full materialization of a branch head from its tree (design 6.11).

Content comes from the tree and the objects it references; history is the changesets of the
head's first-parent chain; lineage and `updated_revision` are folded from that history with
the same rules as the incremental path (`rows.next_lineage`), so both produce equal rows.
Derived values are then computed for every Record with the evaluation the incremental path
uses (`materialize.derived`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Schema
from lorebase_core.tree import PARTITION_SCHEMA, Tree
from lorebase_store.bulk import chunks
from lorebase_store.commit.schema import (
    compile_schema,
    element_from_body,
    schema_from_elements,
)
from lorebase_store.indexes import BranchIndexes
from lorebase_store.materialize.derived import DerivedValues
from lorebase_store.materialize.rows import (
    PartitionType,
    next_lineage,
    parse_partition,
    record_title,
    search_weights,
    split_link_key,
)
from lorebase_store.nodestore import NodeRepository
from lorebase_store.objectstore import PgObjectStore
from lorebase_store.repositories.materialized import (
    LinkRow,
    MaterializedRepository,
    RecordRow,
    SchemaRow,
)
from lorebase_store.repositories.refs import RefRepository
from lorebase_store.repositories.revisions import RevisionRepository, RevisionRow
from lorebase_store.repositories.scope import scope_to_domain
from lorebase_store.runtime import StoreRuntime


@dataclass(frozen=True, slots=True)
class RebuildReport:
    """What a rebuild wrote."""

    branch: str
    head: bytes
    records: int
    links: int


@dataclass(frozen=True, slots=True)
class _Folded:
    """Lineage inputs of one element folded from its history."""

    provenance: dict[str, str]
    revision_id: bytes
    at: datetime


async def rebuild(
    session: AsyncSession, domain_id: str, branch: str, *, runtime: StoreRuntime
) -> RebuildReport:
    """Replace the materialized head of `branch` with one computed from its tree."""
    await scope_to_domain(session, domain_id)
    ref = await RefRepository(session, domain_id).require(branch)
    head = await RevisionRepository(session, domain_id).require(ref.head)
    repository = MaterializedRepository(session, domain_id, branch, writes=runtime.settings.writes)
    await repository.clear()
    return await materialize_revision(session, domain_id, branch, head, runtime=runtime)


async def load_schema(
    session: AsyncSession, domain_id: str, tree: Tree, *, runtime: StoreRuntime
) -> tuple[Schema, bytes]:
    """Schema of a tree and its schema partition hash."""
    settings = runtime.settings
    nodes = NodeRepository(
        session, domain_id, writes=settings.writes, batch=settings.nodes.prefetch_batch
    )
    objects = PgObjectStore(
        session, domain_id, writes=settings.writes, batch=settings.materialize.read_batch_rows
    )
    ref = tree.partitions[PARTITION_SCHEMA]
    hashes: list[bytes] = []
    async for entries in nodes.scan(ref.root):
        hashes.extend(digest for _, digest in entries)
    bodies = await objects.get_many(hashes)
    elements = [element_from_body(bodies[h].object_type, bodies[h].body) for h in hashes]
    return schema_from_elements(elements), ref.root


async def materialize_revision(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    head: RevisionRow,
    *,
    runtime: StoreRuntime,
) -> RebuildReport:
    """Write the materialized head of an empty branch from the tree of `head`."""
    settings = runtime.settings
    batch = settings.materialize.read_batch_rows
    repository = MaterializedRepository(session, domain_id, branch, writes=settings.writes)
    nodes = NodeRepository(
        session, domain_id, writes=settings.writes, batch=settings.nodes.prefetch_batch
    )
    objects = PgObjectStore(session, domain_id, writes=settings.writes, batch=batch)
    tree = await nodes.load_tree(head.root)
    schema, schema_hash = await load_schema(session, domain_id, tree, runtime=runtime)
    indexes = BranchIndexes(session, domain_id, branch, settings=settings.indexes)
    await indexes.retire(schema)
    await repository.set_schema(SchemaRow(schema_hash, compile_schema(schema)))
    await repository.rebuild_history(head.id)
    records = links = 0
    for partition, ref in sorted(tree.partitions.items()):
        parsed = parse_partition(partition)
        if parsed.type is PartitionType.SCHEMA:
            continue
        entries: dict[str, bytes] = {}
        async for scanned in nodes.scan(ref.root):
            entries.update(scanned)
        folded = await _fold_history(repository, partition, batch=batch)
        for chunk in chunks(sorted(entries), batch):
            bodies = await objects.get_many(entries[key] for key in chunk)
            values = {key: dict(bodies[entries[key]].body.get("values", {})) for key in chunk}
            if parsed.type is PartitionType.KIND:
                await repository.upsert_records(
                    [
                        _record_row(
                            parsed.element_id,
                            key,
                            values[key],
                            entries[key],
                            schema,
                            folded.get(key),
                            head,
                        )
                        for key in chunk
                    ]
                )
            else:
                await repository.upsert_links(
                    [
                        _link_row(
                            parsed.element_id, key, values[key], entries[key], folded.get(key)
                        )
                        for key in chunk
                    ]
                )
        if parsed.type is PartitionType.KIND:
            records += len(entries)
            await repository.refresh_search(
                parsed.element_id,
                search_weights(schema.kinds.get(parsed.element_id)),
                config=settings.materialize.text_search_config,
            )
        else:
            links += len(entries)
    if settings.materialize.derivations_enabled:
        derived = DerivedValues(
            repository,
            schema,
            limits=runtime.schema.expression_limits,
            now=head.created_at,
            batch=batch,
            max_incremental=settings.materialize.derivation_max_incremental_records,
        )
        await derived.recompute_all()
        for kind_id in sorted(derived.kinds):
            if derived.searchable(kind_id):
                await repository.refresh_search(
                    kind_id,
                    search_weights(schema.kinds.get(kind_id)),
                    config=settings.materialize.text_search_config,
                )
    await indexes.ensure(schema)
    return RebuildReport(branch=branch, head=head.id, records=records, links=links)


async def _fold_history(
    repository: MaterializedRepository, partition: str, *, batch: int
) -> dict[str, _Folded]:
    """Per element still present, the union of its provenance maps since it was last created."""
    folded: dict[str, _Folded] = {}
    async for steps in repository.stream_history_steps(partition, batch=batch):
        for step in steps:
            if step.new_hash is None:
                folded.pop(step.element_key, None)
                continue
            previous = folded.get(step.element_key)
            provenance = {} if previous is None else previous.provenance
            provenance.update(step.provenance or {})
            folded[step.element_key] = _Folded(provenance, step.revision_id, step.at)
    return folded


def _record_row(
    kind_id: str,
    key: str,
    values: Mapping[str, Any],
    digest: bytes,
    schema: Schema,
    folded: _Folded | None,
    head: RevisionRow,
) -> RecordRow:
    return RecordRow(
        kind_id=kind_id,
        key=key,
        values=dict(values),
        title=record_title(schema.kinds.get(kind_id), values),
        object_hash=digest,
        lineage=next_lineage(None, None if folded is None else folded.provenance, values),
        updated_revision=head.id if folded is None else folded.revision_id,
        updated_at=head.created_at if folded is None else folded.at,
    )


def _link_row(
    relation_id: str,
    key: str,
    values: Mapping[str, Any],
    digest: bytes,
    folded: _Folded | None,
) -> LinkRow:
    from_key, to_key = split_link_key(key)
    return LinkRow(
        relation_id=relation_id,
        from_key=from_key,
        to_key=to_key,
        values=dict(values),
        object_hash=digest,
        lineage=next_lineage(None, None if folded is None else folded.provenance, values),
    )
