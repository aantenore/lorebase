"""Authorization of Proposal reads against both of their branches (design 10.2, 10.3).

A Proposal and its Conflicts carry content of its source and target branches (Conflicts hold
the base, ours and theirs values), so reading them follows the read rules of
`services.reads` on both refs:

- `data.read` on the source and on the target branch shows every data Conflict;
- otherwise grants narrowed to Kinds (which cover every branch and Revision) show the Conflicts
  of those Kinds only: Records of those Kinds, and Links of Relations between two of them;
- schema Conflicts need `schema.read` on both branches, through grants not narrowed to a Kind;
- callers with neither get 403, also for unknown Proposals when their grants are narrowed.

Listings keep the Proposals the caller may read in this sense.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from lorebase_core.model import Schema
from lorebase_core.tree import PARTITION_SCHEMA, kind_partition, relation_partition
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.services.reads import ReadGuard
from lorebase_store.repositories import EVERY_PARTITION, PartitionFilter, ProposalRow


@dataclass(frozen=True)
class ConflictScope:
    """The Conflicts of a Proposal a caller may read: those of every Kind (`kinds` is None)
    or of the Kinds named (api names), and schema Conflicts when `schema` is true."""

    kinds: frozenset[str] | None = None
    schema: bool = True

    @property
    def everything(self) -> bool:
        return self.kinds is None and self.schema


FULL_SCOPE = ConflictScope()


async def _both(guard: ReadGuard, proposal: ProposalRow) -> bool:
    return await guard.may(proposal.source_ref) and await guard.may(proposal.target_ref)


async def conflict_scope(data: ReadGuard, proposal: ProposalRow) -> ConflictScope:
    """What `data` (a `data.read` guard) may read of `proposal`; 403 when nothing."""
    schema = replace(data, permissions=(Permission.SCHEMA_READ,), kind=None)
    schema_readable = await _both(schema, proposal)
    if await _both(data, proposal):
        return ConflictScope(schema=schema_readable)
    kinds = await data.granted_kinds()
    if not kinds:
        raise data.denied()
    return ConflictScope(kinds=kinds, schema=schema_readable)


async def readable_proposals(data: ReadGuard, rows: Sequence[ProposalRow]) -> list[ProposalRow]:
    """The Proposals of `rows` the caller of `data` may read (see module doc)."""
    if await data.wide() or await data.granted_kinds():
        return list(rows)
    return [row for row in rows if await _both(data, row)]


def visible_partitions(scope: ConflictScope, schemas: Iterable[Schema]) -> PartitionFilter:
    """The partitions holding Conflicts `scope` shows, given the schemas of the attempt's
    Revisions (Kind and Relation ids are stable across them)."""
    if scope.everything:
        return EVERY_PARTITION
    if scope.kinds is None:
        return PartitionFilter(exclude=frozenset({PARTITION_SCHEMA}))
    seen = list(schemas)
    kind_ids = {
        kind.id for schema in seen for kind in schema.kinds.values() if kind.api_name in scope.kinds
    }
    partitions = {kind_partition(kind_id) for kind_id in kind_ids}
    partitions |= {
        relation_partition(relation.id)
        for schema in seen
        for relation in schema.relations.values()
        if relation.from_kind in kind_ids and relation.to_kind in kind_ids
    }
    if scope.schema:
        partitions.add(PARTITION_SCHEMA)
    return PartitionFilter(include=frozenset(partitions))
