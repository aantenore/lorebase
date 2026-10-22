"""Verification of a materialized head against its tree (design 6.11, invariant I4).

Recomputes what the tree at the branch head says the materialized rows must be and compares:
the schema partition hash, the set of `(key, object_hash)` of every Kind and Relation
partition, and the per-partition counts. With `deep=True`, Record and Link values and Record
titles are also recomputed from the stored objects. With `derived=True`, derived values are
recomputed from the materialized Records and Links (`materialize.derived`, nothing written) and
compared with the stored ones, and so are the Links of materialized path Derivations
(`materialize.paths`), which the tree comparison leaves out.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.expr.sensitivity import with_tags
from lorebase_core.model import Schema
from lorebase_core.tree import PARTITION_SCHEMA, kind_partition, link_key, relation_partition
from lorebase_store.commit.schema import load_compiled
from lorebase_store.materialize.derived import DerivedValues
from lorebase_store.materialize.paths import PathLinks
from lorebase_store.materialize.rebuild import load_schema
from lorebase_store.materialize.rows import (
    PartitionType,
    parse_partition,
    record_title,
)
from lorebase_store.materialize.sensitivity import reference_tags
from lorebase_store.nodestore import NodeRepository
from lorebase_store.objectstore import PgObjectStore
from lorebase_store.repositories.materialized import MaterializedRepository
from lorebase_store.repositories.refs import RefRepository
from lorebase_store.repositories.revisions import RevisionRepository
from lorebase_store.repositories.scope import scope_to_domain
from lorebase_store.runtime import StoreRuntime


class DivergenceReason(StrEnum):
    SCHEMA_MISSING = "schema_missing"
    SCHEMA_MISMATCH = "schema_mismatch"
    MISSING_ROW = "missing_row"
    EXTRA_ROW = "extra_row"
    HASH_MISMATCH = "hash_mismatch"
    VALUES_MISMATCH = "values_mismatch"
    COUNT_MISMATCH = "count_mismatch"
    DERIVED_MISMATCH = "derived_mismatch"
    SENSITIVITY_STALE = "sensitivity_stale"
    """A title materialized before the default branch tagged the title Attribute: stale,
    not corrupt; the next change of the Kind on the branch, or a rebuild, recomputes it."""


@dataclass(frozen=True, slots=True)
class Divergence:
    """One difference between the tree-derived view and the materialized rows.

    `expected` comes from the tree, `actual` from the materialized rows.
    """

    reason: DivergenceReason
    partition: str
    key: str | None = None
    expected: Any = None
    actual: Any = None


@dataclass
class VerifyReport:
    domain_id: str
    branch: str
    head: bytes
    partitions: int = 0
    rows: int = 0
    divergences: list[Divergence] = field(default_factory=list)
    truncated: bool = False
    _limit: int = 1000

    @property
    def ok(self) -> bool:
        return not self.divergences

    def add(self, divergence: Divergence) -> None:
        if len(self.divergences) >= self._limit:
            self.truncated = True
            return
        self.divergences.append(divergence)


async def verify(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    *,
    runtime: StoreRuntime,
    deep: bool = False,
    derived: bool = False,
) -> VerifyReport:
    """Compare the materialized head of `branch` with the tree at its head (and, with
    `derived`, its derived values with recomputed ones)."""
    settings = runtime.settings
    batch = settings.materialize.read_batch_rows
    await scope_to_domain(session, domain_id)
    ref = await RefRepository(session, domain_id).require(branch)
    head = await RevisionRepository(session, domain_id).require(ref.head)
    report = VerifyReport(
        domain_id, branch, head.id, _limit=settings.materialize.verify_max_divergences
    )
    repository = MaterializedRepository(session, domain_id, branch, writes=settings.writes)
    nodes = NodeRepository(
        session, domain_id, writes=settings.writes, batch=settings.nodes.prefetch_batch
    )
    objects = PgObjectStore(session, domain_id, writes=settings.writes, batch=batch)
    tree = await nodes.load_tree(head.root)

    schema, schema_hash = await load_schema(session, domain_id, tree, runtime=runtime)
    tagged = with_tags(schema, await reference_tags(session, domain_id, branch, runtime=runtime))
    stored_schema = await repository.schema()
    if stored_schema is None:
        report.add(Divergence(DivergenceReason.SCHEMA_MISSING, PARTITION_SCHEMA))
    elif stored_schema.schema_hash != schema_hash or (
        deep and load_compiled(stored_schema.compiled) != schema
    ):
        report.add(
            Divergence(
                DivergenceReason.SCHEMA_MISMATCH,
                PARTITION_SCHEMA,
                expected=schema_hash.hex(),
                actual=stored_schema.schema_hash.hex(),
            )
        )

    counts = {kind_partition(k): n for k, n in (await repository.record_counts()).items()}
    counts.update({relation_partition(r): n for r, n in (await repository.link_counts()).items()})
    partitions = {p: ref.count for p, ref in tree.partitions.items() if p != PARTITION_SCHEMA}
    for partition in sorted(partitions.keys() | counts.keys()):
        report.partitions += 1
        expected_count = partitions.get(partition, 0)
        actual_count = counts.get(partition, 0)
        if expected_count != actual_count:
            report.add(
                Divergence(
                    DivergenceReason.COUNT_MISMATCH,
                    partition,
                    expected=expected_count,
                    actual=actual_count,
                )
            )
        expected: dict[str, bytes] = {}
        if partition in tree.partitions:
            async for entries in nodes.scan(tree.partitions[partition].root):
                expected.update(entries)
        actual = await _materialized(repository, partition, batch=batch)
        report.rows += len(actual)
        _compare_hashes(report, partition, expected, {k: v[0] for k, v in actual.items()})
        if deep:
            await _compare_values(report, partition, expected, actual, objects, tagged, schema)
    if derived:
        await _compare_derived(report, repository, schema, runtime, head.created_at)
        await _compare_paths(report, repository, schema, runtime)
    return report


async def _compare_derived(
    report: VerifyReport,
    repository: MaterializedRepository,
    schema: Schema,
    runtime: StoreRuntime,
    at: Any,
) -> None:
    """Recompute every derived value without writing and compare with the stored ones."""
    settings = runtime.settings.materialize
    recomputed = DerivedValues(
        repository,
        schema,
        limits=runtime.schema.expression_limits,
        now=at,
        batch=settings.read_batch_rows,
        max_incremental=settings.derivation_max_incremental_records,
        fanout=runtime.settings.traverse.path_fanout,
        write=False,
    )
    await recomputed.recompute_all()
    for kind_id in sorted(await repository.record_counts()):
        async for rows in repository.stream_records(kind_id, batch=settings.read_batch_rows):
            for row in rows:
                want = recomputed.expected.get((kind_id, row.key), {})
                if row.derived != want:
                    report.add(
                        Divergence(
                            DivergenceReason.DERIVED_MISMATCH,
                            kind_partition(kind_id),
                            row.key,
                            expected=want,
                            actual=row.derived,
                        )
                    )


async def _compare_paths(
    report: VerifyReport, repository: MaterializedRepository, schema: Schema, runtime: StoreRuntime
) -> None:
    """Recompute the Links of every materialized path Derivation without writing and compare
    them with the stored ones (and report Links of paths no longer materialized)."""
    settings = runtime.settings
    paths = PathLinks(
        repository,
        schema,
        fanout=settings.traverse.path_fanout,
        batch=settings.materialize.read_batch_rows,
        max_incremental=settings.materialize.derivation_max_incremental_records,
        write=False,
    )
    known = {path.id for path in paths.paths}
    for path_id, count in sorted((await repository.link_counts(derived=True)).items()):
        if path_id not in known:
            report.add(
                Divergence(
                    DivergenceReason.DERIVED_MISMATCH,
                    relation_partition(path_id),
                    expected=0,
                    actual=count,
                )
            )
    for path in paths.paths:
        await paths.compute_all(path)
        want = paths.expected.get(path.id, set())
        got = await paths.stored(path)
        for from_key, to_key in sorted(want ^ got):
            report.add(
                Divergence(
                    DivergenceReason.DERIVED_MISMATCH,
                    relation_partition(path.id),
                    link_key(from_key, to_key),
                    expected=(from_key, to_key) in want,
                    actual=(from_key, to_key) in got,
                )
            )


async def _materialized(
    repository: MaterializedRepository, partition: str, *, batch: int
) -> dict[str, tuple[bytes, dict[str, Any] | None, str | None]]:
    """`key -> (object_hash, values, title)` of a partition's materialized rows."""
    parsed = parse_partition(partition)
    rows: dict[str, tuple[bytes, dict[str, Any] | None, str | None]] = {}
    if parsed.type is PartitionType.KIND:
        async for records in repository.stream_records(parsed.element_id, batch=batch):
            rows.update((r.key, (r.object_hash, r.values, r.title)) for r in records)
    else:
        async for links in repository.stream_links(parsed.element_id, batch=batch):
            rows.update(
                (link_key(r.from_key, r.to_key), (r.object_hash, r.values, None)) for r in links
            )
    return rows


def _compare_hashes(
    report: VerifyReport, partition: str, expected: Mapping[str, bytes], actual: Mapping[str, bytes]
) -> None:
    for key in sorted(expected.keys() | actual.keys()):
        want, got = expected.get(key), actual.get(key)
        if want == got:
            continue
        if got is None:
            reason = DivergenceReason.MISSING_ROW
        elif want is None:
            reason = DivergenceReason.EXTRA_ROW
        else:
            reason = DivergenceReason.HASH_MISMATCH
        report.add(
            Divergence(
                reason,
                partition,
                key,
                expected=None if want is None else want.hex(),
                actual=None if got is None else got.hex(),
            )
        )


async def _compare_values(
    report: VerifyReport,
    partition: str,
    expected: Mapping[str, bytes],
    actual: Mapping[str, tuple[bytes, dict[str, Any] | None, str | None]],
    objects: PgObjectStore,
    schema: Schema,
    own: Schema | None = None,
) -> None:
    """`schema` has the default branch's tags added; `own` is the branch's own schema, which
    tells a stale title (`SENSITIVITY_STALE`) from a wrong one."""
    common = sorted(expected.keys() & actual.keys())
    bodies = await objects.get_many(expected[key] for key in common)
    parsed = parse_partition(partition)
    kind = schema.kinds.get(parsed.element_id) if parsed.type is PartitionType.KIND else None
    for key in common:
        body = bodies.get(expected[key])
        want_values = {} if body is None else dict(body.body.get("values", {}))
        _, got_values, got_title = actual[key]
        want_title = record_title(kind, want_values) if kind is not None else None
        if got_values != want_values or got_title != want_title:
            own_kind = None if own is None else own.kinds.get(parsed.element_id)
            stale = (
                got_values == want_values
                and own_kind is not None
                and got_title == record_title(own_kind, want_values)
            )
            report.add(
                Divergence(
                    DivergenceReason.SENSITIVITY_STALE
                    if stale
                    else DivergenceReason.VALUES_MISMATCH,
                    partition,
                    key,
                    expected={"values": want_values, "title": want_title},
                    actual={"values": got_values, "title": got_title},
                )
            )
