"""Batches of a branch built from its tree, and of a schema change reconciled after a copy
(`materialize.background`).

Each function runs one batch in the caller's transaction and returns the rows it handled
with the cursor of the next batch (`None`: the phase is done). The caller holds the branch
lock. They write the branch's own rows (`full_layout`): a branch being built is not read
through its materialized rows meanwhile, and a reconciled branch is full.

A tree build does in pieces what `rebuild.materialize_revision` does in one transaction:

- `clear_batch`: deletes rows the branch held before (an overlay detached from its base);
- `history_batch`: the changesets of the head's first-parent chain, a few Revisions at a
  time (`depth` from the head), then the schema row;
- `rows_batch`: Records and Links of the head's tree, whole leaves at a time
  (`NodeRepository.scan_from`), with lineage folded from the history, titles and search
  vectors;
- `derived_batch` and `paths_batch`: every derived value, Derivation by Derivation, and the
  pairs of the materialized paths, start key by start key.

A schema change made while an overlay was copied (`apply.apply_changeset`) is reconciled
after the switch with what a schema change recomputes on a full branch: `prune_batch` drops
derived values and path Links the schema no longer has, then `derived_batch`, `paths_batch`
and `search_batch` (titles and search vectors of the Kinds whose settings changed).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.expr import ExprLimits
from lorebase_core.expr.sensitivity import with_tags
from lorebase_core.model import Schema
from lorebase_store.bulk import chunks
from lorebase_store.layout import MAT_HISTORY, MAT_LINKS, MAT_RECORDS, full_layout
from lorebase_store.materialize.derived import DerivedValues, plan_derivations
from lorebase_store.materialize.paths import PathLinks
from lorebase_store.materialize.rebuild import fold_history, link_row, load_schema, record_row
from lorebase_store.materialize.rows import (
    PartitionType,
    parse_partition,
    record_title,
    search_weights,
)
from lorebase_store.materialize.sensitivity import reference_tags
from lorebase_store.nodestore import NodeRepository
from lorebase_store.objectstore import PgObjectStore
from lorebase_store.repositories.materialized import DERIVED_LINK_PREFIX, SchemaRow
from lorebase_store.repositories.materialized import MaterializedRepository as Rows
from lorebase_store.repositories.revisions import RevisionRow
from lorebase_store.runtime import StoreRuntime

Cursor = list[Any] | None


@dataclass(frozen=True, slots=True)
class Batch:
    """What one batch did: rows handled and where the next one starts (`None`: done)."""

    rows: int
    cursor: Cursor


def own_rows(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    runtime: StoreRuntime,
    *,
    generation: int | None = None,
) -> Rows:
    """The branch's own rows, whatever its ref says (`full_layout`)."""
    return Rows(
        session,
        domain_id,
        branch,
        writes=runtime.settings.writes,
        layout=full_layout(domain_id, branch),
        generation=generation,
    )


async def branch_schema(rows: Rows) -> Schema | None:
    """The schema stored for the branch, if any."""
    stored = await rows.schema()
    if stored is None:
        return None
    # Imported here: the commit package imports the materializer.
    from lorebase_store.commit.schema import load_compiled

    return load_compiled(stored.compiled)


# Tree build ---------------------------------------------------------------------------------


async def clear_batch(session: AsyncSession, domain_id: str, branch: str, *, limit: int) -> Batch:
    """Delete up to `limit` rows the branch holds (Records, Links, history), table by
    table; done when none is left."""
    for table in (MAT_RECORDS, MAT_LINKS, MAT_HISTORY):
        key = ", ".join(c.name for c in table.primary_key.columns)
        deleted = await session.execute(
            text(
                f"DELETE FROM {table.name} WHERE ({key}) IN (SELECT {key} FROM {table.name} "
                "WHERE domain_id = :domain AND branch = :branch LIMIT :limit)"
            ),
            {"domain": domain_id, "branch": branch, "limit": limit},
        )
        count = int(getattr(deleted, "rowcount", 0) or 0)
        if count:
            return Batch(count, [])
    return Batch(0, None)


_CHAIN_HISTORY = text(
    """
    WITH RECURSIVE chain(id, depth) AS (
        SELECT CAST(:head AS bytea), 0
        UNION ALL
        SELECT p.parent_id, chain.depth + 1
        FROM chain
        JOIN revision_parents p
          ON p.domain_id = :domain AND p.revision_id = chain.id AND p.position = 0
    ),
    sized AS (
        SELECT chain.id, chain.depth, counted.entries,
               sum(counted.entries) OVER (ORDER BY chain.depth) AS upto
        FROM chain
        CROSS JOIN LATERAL (
            SELECT count(*) AS entries FROM changesets c
            WHERE c.domain_id = :domain AND c.revision_id = chain.id
        ) counted
        WHERE chain.depth >= :from_depth
    ),
    picked AS (
        SELECT id, depth FROM sized WHERE upto - entries < :limit
    ),
    inserted AS (
        INSERT INTO mat_history
            (domain_id, branch, partition, element_key, revision_id, at, old_hash, new_hash,
             provenance)
        SELECT :domain, :branch, c.partition, c.element_key, c.revision_id, r.created_at,
               c.old_hash, c.new_hash, c.provenance
        FROM picked
        JOIN revisions r ON r.domain_id = :domain AND r.id = picked.id
        JOIN changesets c ON c.domain_id = :domain AND c.revision_id = picked.id
        ON CONFLICT DO NOTHING
        RETURNING 1
    )
    SELECT (SELECT count(*) FROM inserted), (SELECT max(depth) FROM picked)
    """
)


async def history_batch(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    head: RevisionRow,
    cursor: Cursor,
    *,
    limit: int,
    runtime: StoreRuntime,
) -> Batch:
    """Copy the changesets of the next Revisions of the head's first-parent chain (cursor:
    `[depth]`, 0 being the head) into the branch history, about `limit` entries (at least one
    Revision). When none is left, store the schema of the head's tree."""
    depth = 0 if not cursor else int(cursor[0])
    found = (
        await session.execute(
            _CHAIN_HISTORY,
            {
                "head": head.id,
                "domain": domain_id,
                "branch": branch,
                "from_depth": depth,
                "limit": limit,
            },
        )
    ).one()
    inserted, last = int(found[0] or 0), found[1]
    if last is not None:
        return Batch(inserted, [int(last) + 1])
    settings = runtime.settings
    nodes = NodeRepository(
        session, domain_id, writes=settings.writes, batch=settings.nodes.prefetch_batch
    )
    tree = await nodes.load_tree(head.root)
    schema, schema_hash = await load_schema(session, domain_id, tree, runtime=runtime)
    # Imported here: the commit package imports the materializer.
    from lorebase_store.commit.schema import compile_schema

    await own_rows(session, domain_id, branch, runtime).set_schema(
        SchemaRow(schema_hash, compile_schema(schema))
    )
    return Batch(0, None)


async def rows_batch(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    head: RevisionRow,
    cursor: Cursor,
    *,
    limit: int,
    runtime: StoreRuntime,
) -> Batch:
    """Write the next Records or Links of the head's tree, whole leaves of one partition at
    a time (cursor: `[partition, leaf path]`)."""
    settings = runtime.settings
    read_batch = settings.materialize.read_batch_rows
    nodes = NodeRepository(
        session, domain_id, writes=settings.writes, batch=settings.nodes.prefetch_batch
    )
    tree = await nodes.load_tree(head.root)
    partitions = [
        p for p in sorted(tree.partitions) if parse_partition(p).type is not PartitionType.SCHEMA
    ]
    if cursor:
        partition, after = str(cursor[0]), cursor[1]
    elif partitions:
        partition, after = partitions[0], None
    else:
        return Batch(0, None)
    rows = own_rows(session, domain_id, branch, runtime, generation=head.generation)
    schema = await branch_schema(rows)
    if schema is None:
        raise RuntimeError(f"branch {branch!r} has no schema row: its history is not built")
    tagged = with_tags(schema, await reference_tags(session, domain_id, branch, runtime=runtime))
    objects = PgObjectStore(session, domain_id, writes=settings.writes, batch=read_batch)
    entries, last = await nodes.scan_from(tree.partitions[partition].root, after=after, limit=limit)
    parsed = parse_partition(partition)
    hashes = dict(entries)
    keys = sorted(hashes)
    folded = await fold_history(rows, partition, batch=read_batch, keys=keys) if keys else {}
    for chunk in chunks(keys, read_batch):
        bodies = await objects.get_many(hashes[key] for key in chunk)
        values = {key: dict(bodies[hashes[key]].body.get("values", {})) for key in chunk}
        if parsed.type is PartitionType.KIND:
            await rows.upsert_records(
                [
                    record_row(
                        parsed.element_id,
                        key,
                        values[key],
                        hashes[key],
                        tagged,
                        folded.get(key),
                        head,
                    )
                    for key in chunk
                ]
            )
            await rows.refresh_search(
                parsed.element_id,
                search_weights(tagged.kinds.get(parsed.element_id)),
                config=settings.materialize.text_search_config,
                keys=chunk,
            )
        else:
            await rows.upsert_links(
                [
                    link_row(parsed.element_id, key, values[key], hashes[key], folded.get(key))
                    for key in chunk
                ]
            )
    if last is not None:
        return Batch(len(keys), [partition, last])
    following = [p for p in partitions if p > partition]
    return Batch(len(keys), [following[0], None] if following else None)


# Derived values and paths (tree build and reconciliation) -----------------------------------


async def _next_keys(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    kind_id: str,
    after: str | None,
    limit: int,
    *,
    path_id: str | None = None,
) -> list[str]:
    """The next `limit` keys of a Kind's Records after `after`, with `path_id` also the
    start keys stored for that path (a start Kind changed by a schema change leaves them)."""
    params: dict[str, Any] = {"domain": domain_id, "branch": branch, "kind": kind_id}
    params["limit"] = limit
    # Separate statements with and without a start: a range condition the index serves.
    records = (
        "SELECT key FROM mat_records WHERE domain_id = :domain AND branch = :branch "
        "AND kind_id = :kind"
    )
    if after is not None:
        params["after"] = after
        records += " AND key > :after"
    if path_id is None:
        sql = f"{records} ORDER BY key LIMIT :limit"
    else:
        params["path"] = path_id
        stored = (
            "SELECT from_key FROM mat_links WHERE domain_id = :domain AND branch = :branch "
            "AND relation_id = :path"
        )
        if after is not None:
            stored += " AND from_key > :after"
        sql = (
            f"SELECT key FROM (({records} ORDER BY key LIMIT :limit) UNION "
            f"({stored} ORDER BY from_key LIMIT :limit)) k ORDER BY key LIMIT :limit"
        )
    return [str(key) for (key,) in await session.execute(text(sql), params)]


async def derived_batch(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    head: RevisionRow,
    cursor: Cursor,
    *,
    limit: int,
    runtime: StoreRuntime,
    expression_limits: ExprLimits,
) -> Batch:
    """Recompute one Derivation for the next `limit` Records of its Kind (cursor: `[step,
    last key]`, steps in evaluation order), and the search vectors whose values changed."""
    settings = runtime.settings
    if not settings.materialize.derivations_enabled:
        return Batch(0, None)
    rows = own_rows(session, domain_id, branch, runtime)
    schema = await branch_schema(rows)
    if schema is None:
        return Batch(0, None)
    derived = DerivedValues(
        rows,
        schema,
        limits=expression_limits,
        now=head.created_at,
        batch=settings.materialize.read_batch_rows,
        max_incremental=settings.materialize.derivation_max_incremental_records,
        fanout=settings.traverse.path_fanout,
    )
    index, after = (0, None) if not cursor else (int(cursor[0]), cursor[1])
    while index < len(derived.steps):
        step = derived.steps[index]
        keys = await _next_keys(session, domain_id, branch, step.kind.id, after, limit)
        if not keys:
            index, after = index + 1, None
            continue
        changed = await derived.recompute_step(step, keys)
        if changed and step.attribute.searchable:
            tagged = with_tags(
                schema, await reference_tags(session, domain_id, branch, runtime=runtime)
            )
            await rows.refresh_search(
                step.kind.id,
                search_weights(tagged.kinds.get(step.kind.id)),
                config=settings.materialize.text_search_config,
                keys=sorted(changed),
            )
        return Batch(len(keys), [index, keys[-1]])
    return Batch(0, None)


async def paths_batch(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    cursor: Cursor,
    *,
    limit: int,
    runtime: StoreRuntime,
) -> Batch:
    """Recompute the pairs of one materialized path for its next `limit` start keys
    (cursor: `[path, last key]`)."""
    settings = runtime.settings
    if not settings.materialize.derivations_enabled:
        return Batch(0, None)
    rows = own_rows(session, domain_id, branch, runtime)
    schema = await branch_schema(rows)
    if schema is None:
        return Batch(0, None)
    paths = PathLinks(
        rows,
        schema,
        fanout=settings.traverse.path_fanout,
        batch=settings.materialize.read_batch_rows,
        max_incremental=settings.materialize.derivation_max_incremental_records,
    )
    index, after = (0, None) if not cursor else (int(cursor[0]), cursor[1])
    while index < len(paths.paths):
        path = paths.paths[index]
        starts = await _next_keys(
            session, domain_id, branch, path.relation.from_kind, after, limit, path_id=path.id
        )
        if not starts:
            index, after = index + 1, None
            continue
        await paths.refresh(path, starts)
        return Batch(len(starts), [index, starts[-1]])
    # Every path is computed with the current fan-out: commits need not recompute them.
    await paths.record_fanout()
    return Batch(0, None)


# Reconciliation -----------------------------------------------------------------------------


_DERIVED_ROWS = (
    "SELECT kind_id, key, derived FROM mat_records "
    "WHERE domain_id = :domain AND branch = :branch AND derived <> CAST('{{}}' AS jsonb){after} "
    "ORDER BY kind_id, key LIMIT :limit"
)
_SET_DERIVED = text(
    "UPDATE mat_records SET derived = :derived WHERE domain_id = :domain AND branch = :branch "
    "AND kind_id = :kind AND key = :key"
).bindparams(bindparam("derived", type_=JSONB))
_STALE_PATH_LINKS = text(
    "DELETE FROM mat_links WHERE (domain_id, branch, relation_id, from_key, to_key) IN ("
    "SELECT domain_id, branch, relation_id, from_key, to_key FROM mat_links "
    "WHERE domain_id = :domain AND branch = :branch AND starts_with(relation_id, :prefix) "
    "AND NOT (relation_id = ANY(CAST(:keep AS text[]))) LIMIT :limit)"
)


async def prune_batch(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    cursor: Cursor,
    *,
    limit: int,
    runtime: StoreRuntime,
    expression_limits: ExprLimits,
) -> Batch:
    """Drop derived values of Attributes that are no longer derived from the next `limit`
    Records holding any (cursor: `["records", kind, key]`), then Links of path Derivations
    that are gone or no longer materialized (`["links"]`)."""
    rows = own_rows(session, domain_id, branch, runtime)
    schema = await branch_schema(rows)
    if schema is None:
        return Batch(0, None)
    if not cursor or cursor[0] == "records":
        keep: dict[str, set[str]] = defaultdict(set)
        if runtime.settings.materialize.derivations_enabled:
            for step in plan_derivations(schema, expression_limits):
                keep[step.kind.id].add(step.attribute.id)
        params: dict[str, Any] = {"domain": domain_id, "branch": branch, "limit": limit}
        after = ""
        if cursor:
            params.update(kind=cursor[1], key=cursor[2])
            after = " AND (kind_id, key) > (:kind, :key)"
        found = (await session.execute(text(_DERIVED_ROWS.format(after=after)), params)).all()
        if found:
            for kind_id, record_key, values in found:
                kept = {k: v for k, v in dict(values).items() if k in keep.get(kind_id, ())}
                if kept != dict(values):
                    await session.execute(
                        _SET_DERIVED,
                        {
                            "derived": kept,
                            "domain": domain_id,
                            "branch": branch,
                            "kind": kind_id,
                            "key": record_key,
                        },
                    )
            last_kind, last_key, _ = found[-1]
            return Batch(len(found), ["records", last_kind, last_key])
    materialized = sorted(p.id for p in schema.path_relations().values() if p.materialized)
    if not runtime.settings.materialize.derivations_enabled:
        materialized = []
    deleted = await session.execute(
        _STALE_PATH_LINKS,
        {
            "domain": domain_id,
            "branch": branch,
            "prefix": DERIVED_LINK_PREFIX,
            "keep": materialized,
            "limit": limit,
        },
    )
    count = int(getattr(deleted, "rowcount", 0) or 0)
    return Batch(count, ["links"] if count else None)


async def search_batch(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    refresh_kinds: Sequence[str],
    cursor: Cursor,
    *,
    limit: int,
    runtime: StoreRuntime,
) -> Batch:
    """Recompute titles and search vectors of the next `limit` Records of the Kinds in
    `refresh_kinds` (cursor: `[kind index, last key]`)."""
    rows = own_rows(session, domain_id, branch, runtime)
    schema = await branch_schema(rows)
    if schema is None:
        return Batch(0, None)
    tagged = with_tags(schema, await reference_tags(session, domain_id, branch, runtime=runtime))
    kinds = sorted(refresh_kinds)
    index, after = (0, None) if not cursor else (int(cursor[0]), cursor[1])
    while index < len(kinds):
        kind_id = kinds[index]
        keys = await _next_keys(session, domain_id, branch, kind_id, after, limit)
        if not keys:
            index, after = index + 1, None
            continue
        kind = tagged.kinds.get(kind_id)
        found = await rows.records(kind_id, keys)
        await rows.update_titles(
            kind_id, [(key, record_title(kind, row.values)) for key, row in sorted(found.items())]
        )
        await rows.refresh_search(
            kind_id,
            search_weights(kind),
            config=runtime.settings.materialize.text_search_config,
            keys=keys,
        )
        return Batch(len(keys), [index, keys[-1]])
    return Batch(0, None)
