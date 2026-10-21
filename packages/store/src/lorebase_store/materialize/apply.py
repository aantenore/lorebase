"""Incremental materialization: apply one Revision's changeset to a branch head (design 6.11).

Runs inside the commit transaction (invariant I4). Rows are written in bulk: one statement
batch per partition and operation, then the derived values the changeset can affect
(`materialize.derived`; every derived value when the schema changed), then one set-based
search vector update per Kind.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.expr import ExprLimits
from lorebase_core.model import Schema
from lorebase_core.revision import ChangesetEntry
from lorebase_store.materialize.derived import AppliedChanges, DerivedValues
from lorebase_store.materialize.rows import (
    PartitionType,
    next_lineage,
    parse_partition,
    record_title,
    search_weights,
    split_link_key,
)
from lorebase_store.repositories.materialized import (
    HistoryRow,
    LinkRow,
    MaterializedRepository,
    RecordRow,
    SchemaRow,
)
from lorebase_store.settings import StoreSettings


@dataclass(frozen=True, slots=True)
class MaterializedChange:
    """A changeset entry with the new object body (`None` when the element is removed)."""

    entry: ChangesetEntry
    body: Mapping[str, Any] | None


async def apply_changeset(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    *,
    revision_id: bytes,
    at: datetime,
    changes: Sequence[MaterializedChange],
    schema: Schema,
    schema_hash: bytes | None,
    refresh_kinds: Sequence[str] = (),
    settings: StoreSettings,
    expression_limits: ExprLimits | None = None,
) -> None:
    """Apply a Revision's changeset to the materialized head of `branch`.

    `schema` is the schema at the Revision; `schema_hash` is its schema partition hash when
    the schema changed. `refresh_kinds` lists Kinds whose title or search settings changed:
    their title and search vectors are recomputed for every Record. `expression_limits`
    bound the evaluation of Derivations (`schema.expression_limits`).
    """
    repository = MaterializedRepository(session, domain_id, branch, writes=settings.writes)
    await repository.insert_history(
        [
            HistoryRow(
                partition=change.entry.partition,
                element_key=change.entry.key,
                revision_id=revision_id,
                at=at,
                old_hash=change.entry.old_hash,
                new_hash=change.entry.new_hash,
                provenance=dict(change.entry.provenance) or None,
            )
            for change in changes
        ]
    )
    if schema_hash is not None:
        await repository.set_schema(SchemaRow(schema_hash, schema.content()))
    by_partition: dict[str, list[MaterializedChange]] = defaultdict(list)
    for change in changes:
        by_partition[change.entry.partition].append(change)
    derived = None
    if settings.materialize.derivations_enabled:
        derived = DerivedValues(
            repository,
            schema,
            limits=expression_limits or ExprLimits(),
            now=at,
            batch=settings.materialize.read_batch_rows,
            max_incremental=settings.materialize.derivation_max_incremental_records,
        )
    applied = AppliedChanges()
    keep_previous = derived is not None and bool(derived.steps)
    written_kinds: list[str] = []
    for partition, group in sorted(by_partition.items()):
        parsed = parse_partition(partition)
        if parsed.type is PartitionType.KIND:
            await _apply_records(
                repository,
                parsed.element_id,
                group,
                schema,
                revision_id,
                at,
                applied,
                keep_previous=keep_previous,
            )
            written_kinds.append(parsed.element_id)
        elif parsed.type is PartitionType.RELATION:
            await _apply_links(repository, parsed.element_id, group)
            applied.links[parsed.element_id].update(split_link_key(c.entry.key) for c in group)
    changed: dict[str, set[str]] = {}
    if derived is not None:
        if schema_hash is not None:
            await derived.prune()
            changed = await derived.recompute_all()
        elif derived.steps:
            changed = await derived.recompute(applied)
    search_config = settings.materialize.text_search_config
    for kind_id in written_kinds:
        if kind_id not in refresh_kinds:
            await repository.refresh_search(
                kind_id,
                search_weights(schema.kinds.get(kind_id)),
                config=search_config,
                revision=revision_id,
            )
    for kind_id, keys in sorted(changed.items()):
        if keys and kind_id not in refresh_kinds and derived and derived.searchable(kind_id):
            await repository.refresh_search(
                kind_id,
                search_weights(schema.kinds.get(kind_id)),
                config=search_config,
                keys=sorted(keys),
            )
    for kind_id in refresh_kinds:
        await refresh_kind(repository, schema, kind_id, settings=settings)


async def refresh_kind(
    repository: MaterializedRepository, schema: Schema, kind_id: str, *, settings: StoreSettings
) -> None:
    """Recompute title and search vector of every Record of a Kind."""
    kind = schema.kinds.get(kind_id)
    async for rows in repository.stream_records(
        kind_id, batch=settings.materialize.read_batch_rows
    ):
        await repository.update_titles(
            kind_id, [(row.key, record_title(kind, row.values)) for row in rows]
        )
    await repository.refresh_search(
        kind_id, search_weights(kind), config=settings.materialize.text_search_config
    )


async def _apply_records(
    repository: MaterializedRepository,
    kind_id: str,
    changes: Sequence[MaterializedChange],
    schema: Schema,
    revision_id: bytes,
    at: datetime,
    applied: AppliedChanges,
    *,
    keep_previous: bool,
) -> None:
    removed = [c.entry.key for c in changes if c.body is None]
    written = [c for c in changes if c.body is not None]
    updated = [c.entry.key for c in written if c.entry.old_hash is not None]
    wanted = [*updated, *removed] if keep_previous else updated
    previous = await repository.records(kind_id, wanted) if wanted else {}
    applied.written[kind_id].update(c.entry.key for c in written)
    applied.removed[kind_id].update(removed)
    if keep_previous:
        for key in [*updated, *removed]:
            row = previous.get(key)
            applied.previous[(kind_id, key)] = None if row is None else row.values
    if removed:
        await repository.delete_records(kind_id, removed)
    if not written:
        return
    kind = schema.kinds.get(kind_id)
    rows: list[RecordRow] = []
    for change in written:
        assert change.body is not None
        assert change.entry.new_hash is not None
        values: dict[str, Any] = dict(change.body.get("values", {}))
        before = previous.get(change.entry.key)
        rows.append(
            RecordRow(
                kind_id=kind_id,
                key=change.entry.key,
                values=values,
                title=record_title(kind, values),
                object_hash=change.entry.new_hash,
                lineage=next_lineage(
                    None if before is None else before.lineage, change.entry.provenance, values
                ),
                updated_revision=revision_id,
                updated_at=at,
            )
        )
    await repository.upsert_records(rows)


async def _apply_links(
    repository: MaterializedRepository, relation_id: str, changes: Sequence[MaterializedChange]
) -> None:
    removed = [split_link_key(c.entry.key) for c in changes if c.body is None]
    written = [c for c in changes if c.body is not None]
    if removed:
        await repository.delete_links(relation_id, removed)
    if not written:
        return
    updated = [split_link_key(c.entry.key) for c in written if c.entry.old_hash is not None]
    previous = await repository.links(relation_id, updated) if updated else {}
    rows: list[LinkRow] = []
    for change in written:
        assert change.body is not None
        assert change.entry.new_hash is not None
        from_key, to_key = split_link_key(change.entry.key)
        values: dict[str, Any] = dict(change.body.get("values", {}))
        before = previous.get((from_key, to_key))
        rows.append(
            LinkRow(
                relation_id=relation_id,
                from_key=from_key,
                to_key=to_key,
                values=values,
                object_hash=change.entry.new_hash,
                lineage=next_lineage(
                    None if before is None else before.lineage, change.entry.provenance, values
                ),
            )
        )
    await repository.upsert_links(rows)
