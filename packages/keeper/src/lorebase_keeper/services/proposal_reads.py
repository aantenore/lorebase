"""Authorization of Proposal reads against both of their branches (design 10.2, 10.3).

A Proposal and its Conflicts carry content of its source and target branches (Conflicts hold
the base, ours and theirs values), so reading them follows the read rules of
`services.reads` on both refs:

- `data.read` on the source and on the target branch shows every data Conflict;
- otherwise grants narrowed to Kinds (which cover every branch and Revision) show the Conflicts
  of those Kinds only: Records of those Kinds, and Links of Relations between two of them;
- schema Conflicts need `schema.read` on both branches, through grants not narrowed to a Kind;
- callers with neither get 403, also for unknown Proposals when their grants are narrowed.

Listings keep the Proposals the caller may read in this sense. Writes follow the same scope:
creating a Proposal needs read access to its target branch, and resolving a Conflict (or
receiving the attempt of a merge) is limited to the Conflicts the caller may read.

Within that scope, the values of a Conflict are shown as reads show them
(`ConflictVisibility`): masked by the caller's clearance on the target branch's schema, without
the Attributes the caller's projection leaves out, and with vault references resolved
(`disclosed`).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel

from lorebase_core.model import Schema
from lorebase_core.tree import PARTITION_SCHEMA, kind_partition, relation_partition
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.context import StoreAccess
from lorebase_keeper.errors import FORBIDDEN, NOT_FOUND, ProblemError
from lorebase_keeper.reading.sensitivity import (
    Disclosure,
    Masking,
    conflict_hidden,
    default_schema,
    masking_for,
)
from lorebase_keeper.reading.visibility import projection
from lorebase_keeper.services.reads import ReadGuard
from lorebase_keeper.settings import SensitivitySettings
from lorebase_store.commit.schema import load_compiled
from lorebase_store.repositories import (
    EVERY_PARTITION,
    ConflictRow,
    MaterializedRepository,
    PartitionFilter,
    ProposalRow,
    scope_to_domain,
)


@dataclass(frozen=True)
class ConflictScope:
    """The Conflicts of a Proposal a caller may read: those of every Kind (`kinds` is None)
    or of the Kinds named (api names), and schema Conflicts when `schema` is true."""

    kinds: frozenset[str] | None = None
    schema: bool = True
    invisible: bool = False
    """The other Kinds are invisible to the caller (design 13.3): a Conflict outside the scope
    answers like an unknown one."""

    @property
    def everything(self) -> bool:
        return self.kinds is None and self.schema


FULL_SCOPE = ConflictScope()
NO_SCOPE = ConflictScope(kinds=frozenset(), schema=False)
"""Shows no Conflict."""


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
    return ConflictScope(
        kinds=kinds, schema=schema_readable, invisible=data.authorizer.invisible_kinds
    )


async def reads_both(data: ReadGuard, proposal: ProposalRow) -> bool:
    """Whether `data` reads both branches of `proposal` (not only some Kinds)."""
    return await _both(data, proposal)


async def visible_scope(data: ReadGuard, proposal: ProposalRow) -> ConflictScope:
    """`conflict_scope`, or `NO_SCOPE` instead of 403: for responses of writes whose
    authorization does not depend on reading the Proposal."""
    try:
        return await conflict_scope(data, proposal)
    except ProblemError as error:
        if error.status_code != 403:
            raise
        return NO_SCOPE


async def readable_proposals(data: ReadGuard, rows: Sequence[ProposalRow]) -> list[ProposalRow]:
    """The Proposals of `rows` the caller of `data` may read (see module doc)."""
    if await data.wide() or await data.granted_kinds():
        return list(rows)
    return [row for row in rows if await _both(data, row)]


async def require_target_read(data: ReadGuard, target_ref: str) -> None:
    """403 unless the caller of `data` may read the branch `target_ref`: `data.read` on it,
    or grants narrowed to Kinds (which cover every branch)."""
    if await data.may(target_ref) or await data.granted_kinds():
        return
    raise data.denied()


def require_visible(
    partitions: PartitionFilter, conflict: ConflictRow, attempt_id: str, *, invisible: bool = False
) -> None:
    """403 for a Conflict stored in a partition outside `partitions`; with `invisible`, the
    404 of a Conflict that is not part of the attempt."""
    if not partitions.allows(conflict.partition, conflict.attribute_id):
        if invisible:
            raise unknown_conflict(conflict.fingerprint, attempt_id)
        raise ProblemError(
            FORBIDDEN,
            f"Conflict {conflict.fingerprint} is outside what the caller may read.",
            extensions={"conflict": conflict.fingerprint},
        )


def visible_partitions(scope: ConflictScope, schemas: Iterable[Schema]) -> PartitionFilter:
    """The partitions holding Conflicts `scope` shows, given the schemas of the attempt's
    Revisions (Kind and Relation ids are stable across them)."""
    if scope.everything:
        return EVERY_PARTITION
    if scope.kinds is None:
        return PartitionFilter(exclude=frozenset({PARTITION_SCHEMA}))
    seen = list(schemas)
    hidden: frozenset[str] = frozenset()
    if scope.invisible:
        projections = [projection(schema, scope.kinds) for schema in seen]
        seen = [p.schema for p in projections]
        # Conflicts about Attributes left out of a visible Kind (a reference to an invisible
        # Kind) are left out too: their values are keys of invisible Records.
        hidden = frozenset().union(*(p.hidden for p in projections))
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
    return PartitionFilter(include=frozenset(partitions), exclude_attributes=hidden)


def unknown_conflict(fingerprint: str, attempt_id: str) -> ProblemError:
    """404 for a Conflict that is not part of the current merge attempt."""
    return ProblemError(
        NOT_FOUND,
        f"Conflict {fingerprint} is not part of the current merge attempt.",
        extensions={"conflict": fingerprint, "attempt": attempt_id},
    )


# Conflict values ----------------------------------------------------------------------------


_CONFLICT_SIDES = ("base", "ours", "theirs")


@dataclass(frozen=True)
class ConflictVisibility:
    """What the caller may see of Conflict values: the target branch's schema (None when it
    is not materialized) and the caller's masking."""

    schema: Schema | None
    masking: Masking
    left_out: frozenset[str] = frozenset()
    """Ids the caller's projection of the schema leaves out (Kinds invisible to it, design
    13.3): Conflicts and automatic resolutions never show them."""

    def hidden(self, payload: Mapping[str, Any]) -> bool:
        return conflict_hidden(payload, self.schema, self.masking)

    def names_left_out(self, value: Any) -> bool:
        if not self.left_out:
            return False
        text = json.dumps(value, default=str)
        return any(i in text for i in self.left_out)

    def scrub(self, payload: dict[str, Any]) -> dict[str, Any]:
        """`payload` without the values of Attributes left out, and without a message or
        details naming something left out."""
        if not self.left_out:
            return payload
        out = dict(payload)
        for name in (*_CONFLICT_SIDES, "values"):
            out[name] = _without(out.get(name), self.left_out)
        if self.names_left_out([out.get("message"), out.get("details")]):
            out["message"] = "The versions differ."
            out["details"] = {}
        return out


def _without(value: Any, ids: frozenset[str]) -> Any:
    """A value map (or an element body holding `values`) without the entries of `ids`."""
    if not isinstance(value, dict):
        return value
    if isinstance(value.get("values"), dict):
        return {**value, "values": _without(value["values"], ids)}
    return {k: v for k, v in value.items() if k not in ids}


async def conflict_visibility(
    store: StoreAccess,
    guard: ReadGuard,
    domain_id: str,
    target: str,
    sensitivity: SensitivitySettings,
    scope: ConflictScope | None = None,
) -> ConflictVisibility:
    """The caller's view of the Conflict values of a Proposal into `target`."""
    writes = store.runtime.settings.writes
    async with store.sessions() as session, session.begin():
        await scope_to_domain(session, domain_id)
        compiled = await MaterializedRepository(session, domain_id, target, writes=writes).schema()
        reference = await default_schema(session, domain_id, store.runtime)
    schema = None if compiled is None else load_compiled(compiled.compiled)
    masking = await masking_for(
        guard.authorizer, guard.principal, domain_id, reference, sensitivity
    )
    left_out: frozenset[str] = frozenset()
    if schema is not None and scope is not None and scope.invisible and scope.kinds is not None:
        left_out = projection(schema, scope.kinds).hidden
    return ConflictVisibility(schema, masking if schema is None else masking.over(schema), left_out)


async def disclosed(
    store: StoreAccess, domain_id: str, seen: ConflictVisibility, model: BaseModel
) -> bytes:
    """The JSON body of `model` (values already masked by `seen`) with the vault references
    left in it resolved as on every read (`Disclosure`)."""
    schemas = () if seen.schema is None else (seen.schema,)
    async with store.sessions() as session, session.begin():
        await scope_to_domain(session, domain_id)
        disclosure = Disclosure(session, domain_id, schemas, seen.masking, store.runtime.vault)
        return await disclosure.model(model)
