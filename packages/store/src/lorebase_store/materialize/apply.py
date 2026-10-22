"""Incremental materialization: apply one Revision's changeset to a branch head (design 6.11).

Runs inside the commit transaction (invariant I4). Rows are written in bulk: one statement
batch per partition and operation, then the derived values the changeset can affect
(`materialize.derived`; every derived value when the schema changed) and the Links of
materialized path Derivations (`materialize.paths`; every one when the schema or
`store.traverse.path_fanout` changed), then one set-based
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
from lorebase_store.materialize.overlay import check_overlay, promote
from lorebase_store.materialize.paths import PathLinks
from lorebase_store.materialize.rows import (
    PartitionType,
    next_lineage,
    next_lineage_revisions,
    parse_partition,
    record_title,
    search_weights,
    split_link_key,
)
from lorebase_store.materialize.sensitivity import branch_tagged_schema
from lorebase_store.repositories.materializations import BranchMaterializationRepository
from lorebase_store.repositories.materialized import (
    HistoryRow,
    LinkRow,
    MaterializedRepository,
    RecordRow,
    SchemaRow,
)
from lorebase_store.repositories.revisions import RevisionRepository
from lorebase_store.runtime import StoreRuntime
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
    runtime: StoreRuntime | None = None,
) -> None:
    """Apply a Revision's changeset to the materialized head of `branch`.

    `schema` is the schema at the Revision; `schema_hash` is its schema partition hash when
    the schema changed. `refresh_kinds` lists Kinds whose title or search settings changed:
    their title and search vectors are recomputed for every Record. `expression_limits`
    bound the evaluation of Derivations (`schema.expression_limits`).

    The caller holds the branch lock. When the schema changes, an overlay over a small base
    is promoted to a full copy first; an overlay over a large base, or one whose background
    copy is queued or running, keeps its overlay rows: the change is applied to the rows the
    changeset writes, and the copy job (queued if needed) recomputes the rest after the
    switch (`reconcile`, `materialize.background`). An overlay over its limits is promoted
    afterwards (`materialize.overlay`); overlays based on the branch get the previous state
    of the rows it changes. A `building` branch raises `BranchMaterializingError`.
    """
    runtime = runtime or StoreRuntime.from_settings(settings)
    # Titles and search vectors use the default branch's tags too (`materialize.sensitivity`).
    tagged = await branch_tagged_schema(session, domain_id, branch, schema, runtime=runtime)
    generation = (await RevisionRepository(session, domain_id).generations([revision_id]))[
        revision_id
    ]
    repository = MaterializedRepository(
        session,
        domain_id,
        branch,
        writes=settings.writes,
        generation=generation,
        lock_timeout_ms=settings.commit.lock_timeout_ms,
    )
    layout = await repository.layout()
    if layout.building:
        # Imported here: the commit package imports the materializer.
        from lorebase_store.commit.errors import BranchMaterializingError

        raise BranchMaterializingError(
            f"branch {branch!r} is being built from its tree in the background; retry when "
            "its materialization is done"
        )
    deferred = False
    if schema_hash is not None and layout.overlay:
        deferred = await defer_schema_change(
            session, domain_id, branch, refresh_kinds=refresh_kinds, at=at, runtime=runtime
        )
        if not deferred:
            await promote(session, domain_id, branch, runtime=runtime, locked=True)
        repository.forget_layout()
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
            fanout=settings.traverse.path_fanout,
        )
    paths = None
    if settings.materialize.derivations_enabled:
        paths = PathLinks(
            repository,
            schema,
            fanout=settings.traverse.path_fanout,
            batch=settings.materialize.read_batch_rows,
            max_incremental=settings.materialize.derivation_max_incremental_records,
        )
    applied = AppliedChanges()
    keep_previous = (derived is not None and bool(derived.steps)) or bool(paths and paths.paths)
    written_kinds: list[str] = []
    for partition, group in sorted(by_partition.items()):
        parsed = parse_partition(partition)
        if parsed.type is PartitionType.KIND:
            await _apply_records(
                repository,
                parsed.element_id,
                group,
                tagged,
                revision_id,
                at,
                applied,
                keep_previous=keep_previous,
            )
            written_kinds.append(parsed.element_id)
        elif parsed.type is PartitionType.RELATION:
            await _apply_links(repository, parsed.element_id, group)
            applied.links[parsed.element_id].update(split_link_key(c.entry.key) for c in group)
    # A deferred schema change recomputes only what the changeset touched; the copy job
    # reconciles every other row after its switch.
    whole = schema_hash is not None and not deferred
    changed: dict[str, set[str]] = {}
    # A change of `store.traverse.path_fanout` recomputes whatever follows it, once.
    stale = schema_hash is None and paths is not None and await paths.fanout_stale(derived)
    if derived is not None:
        if whole:
            await derived.prune()
            changed = await derived.recompute_all()
        elif stale and derived.reads_paths:
            changed = await derived.recompute_all()
        elif derived.steps:
            changed = await derived.recompute(applied)
    if paths is not None:
        if whole or stale:
            if whole:
                await paths.prune()
            await paths.recompute_all()
            await paths.record_fanout()
        elif paths.paths:
            await paths.recompute(applied)
    if deferred:
        refresh_kinds = ()
    search_config = settings.materialize.text_search_config
    for kind_id in written_kinds:
        if kind_id not in refresh_kinds:
            await repository.refresh_search(
                kind_id,
                search_weights(tagged.kinds.get(kind_id)),
                config=search_config,
                revision=revision_id,
            )
    for kind_id, keys in sorted(changed.items()):
        if keys and kind_id not in refresh_kinds and derived and derived.searchable(kind_id):
            await repository.refresh_search(
                kind_id,
                search_weights(tagged.kinds.get(kind_id)),
                config=search_config,
                keys=sorted(keys),
            )
    for kind_id in refresh_kinds:
        await refresh_kind(repository, tagged, kind_id, settings=settings)
    await check_overlay(repository, generation=generation, runtime=runtime)


async def defer_schema_change(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    *,
    refresh_kinds: Sequence[str],
    at: datetime,
    runtime: StoreRuntime,
) -> bool:
    """For a schema change on the overlay `branch`: whether its background copy takes it
    over (`reconcile`) instead of a promotion in the writing transaction. It does when a copy
    is queued or running, or when the base holds more than `async_copy_min_rows` rows (the
    copy is then queued here)."""
    # Imported here: the background jobs import the overlay helpers this module uses.
    from lorebase_store.materialize.background import queue_materialization
    from lorebase_store.materialize.overlay import base_is_large
    from lorebase_store.repositories.refs import RefRepository

    states = BranchMaterializationRepository(session, domain_id)
    if not await states.active(branch):
        ref = await RefRepository(session, domain_id).get(branch)
        if ref is None or ref.base_branch is None:
            return False
        if not await base_is_large(session, domain_id, ref.base_branch, runtime=runtime):
            return False
        await queue_materialization(session, domain_id, ref, at=at)
    await states.add_reconcile(branch, refresh_kinds=refresh_kinds, at=at)
    return True


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
                lineage_revisions=next_lineage_revisions(
                    None if before is None else before.lineage_revisions,
                    change.entry.provenance,
                    revision_id,
                    values,
                    created=change.entry.old_hash is None,
                ),
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
