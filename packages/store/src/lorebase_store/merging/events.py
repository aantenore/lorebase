"""Conflict events of merge attempts (design 6.10, 10.5; `lorebase_store.review_events`).

An attempt is compared with the previous attempt of its Proposal (`conflict_changes`):

- a Conflict open now that was not open before (new, its fingerprint changed, or reported
  again after its resolution was applied) is `conflict.opened`;
- a Conflict open before and resolved now by a stored resolution is `conflict.resolved`
  (`reason: resolution`); one open before and no longer reported (a head moved) is
  `conflict.resolved` (`reason: obsolete`);
- anything else writes nothing: a Conflict open in both, and a resolution carried over by
  fingerprint to a recomputed (stale) attempt, are not events again.

The payload names the Proposal, both branches, the attempt and the Conflict (id, code, Kind or
Relation, element key, Attribute api name, member count), never its values, and for a
resolution its choice and resolver, never a custom value. A Conflict about an Attribute
carries in its `$audience` the tags a reader of that Attribute needs: those of the target,
source and default branch schemas, carried through Derivations (`effective_tags`); an
Attribute none of them knows needs every tag. With the branches, it keeps the event from any
webhook that could not read the Attribute or the source branch (`conflict_in_progress`
applies the same rule to readers).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.expr.limits import ExprLimits
from lorebase_core.expr.sensitivity import Tags, effective_tags, schema_tags
from lorebase_core.model import Schema, Sensitivity
from lorebase_store.materialize.rows import PartitionType, parse_partition
from lorebase_store.reads import head_schema
from lorebase_store.repositories.domains import DomainRepository
from lorebase_store.repositories.events import OutboxRepository
from lorebase_store.repositories.merges import ConflictRow
from lorebase_store.review_events import (
    AUDIENCE_KEY,
    CONFLICT_OPENED,
    CONFLICT_RESOLVED,
    audience,
)
from lorebase_store.runtime import StoreRuntime

RESOLVED_BY_RESOLUTION = "resolution"
RESOLVED_AS_OBSOLETE = "obsolete"
EVERY_TAG: Tags = frozenset(Sensitivity)


@dataclass(frozen=True, slots=True)
class ConflictChange:
    """A Conflict event to write: its type, the Conflict and, when resolved, why."""

    event_type: str
    conflict: ConflictRow
    reason: str | None = None


def conflict_changes(
    previous: Sequence[ConflictRow], current: Sequence[ConflictRow]
) -> list[ConflictChange]:
    """The Conflict events of an attempt with Conflicts `current`, after one with `previous`
    (empty for the first attempt); see the module doc."""
    was_open = {row.fingerprint: row for row in previous if not row.resolved}
    changes: list[ConflictChange] = []
    for row in current:
        if not row.resolved and row.fingerprint not in was_open:
            changes.append(ConflictChange(CONFLICT_OPENED, row))
        elif row.resolved and row.fingerprint in was_open:
            changes.append(ConflictChange(CONFLICT_RESOLVED, row, RESOLVED_BY_RESOLUTION))
    reported = {row.fingerprint for row in current}
    changes.extend(
        ConflictChange(CONFLICT_RESOLVED, row, RESOLVED_AS_OBSOLETE)
        for fingerprint, row in was_open.items()
        if fingerprint not in reported
    )
    return changes


@dataclass(frozen=True)
class SchemaView:
    """The schemas naming a Proposal's Conflicts (target head, then source head) with the
    effective tags of each, the default branch's tags included."""

    schemas: tuple[Schema, ...]
    tags: tuple[Mapping[str, Tags], ...]
    reference: Mapping[str, Tags]

    @classmethod
    def of(
        cls,
        schemas: Sequence[Schema | None],
        reference: Schema | None,
        limits: ExprLimits | None = None,
    ) -> SchemaView:
        found = tuple(s for s in schemas if s is not None)
        extra = schema_tags(reference)
        return cls(found, tuple(effective_tags(s, extra, limits) for s in found), extra)

    def _holder(self, schema: Schema, partition: str) -> Any:
        parsed = parse_partition(partition)
        if parsed.type is PartitionType.KIND:
            return schema.kinds.get(parsed.element_id)
        return schema.relations.get(parsed.element_id)

    def required_tags(self, partition: str, attribute_id: str | None) -> Tags:
        """Tags a reader of the Conflict's Attribute needs; none for a Conflict about a whole
        element or the schema, every tag for an Attribute no schema knows."""
        if attribute_id is None:
            return frozenset()
        try:
            if parse_partition(partition).type is PartitionType.SCHEMA:
                return frozenset()
        except ValueError:
            return EVERY_TAG
        known = False
        tags: set[Sensitivity] = set(self.reference.get(attribute_id, ()))
        for schema, effective in zip(self.schemas, self.tags, strict=True):
            holder = self._holder(schema, partition)
            if holder is None or attribute_id not in holder.attributes:
                continue
            known = True
            tags |= set(holder.attributes[attribute_id].sensitivity)
            tags |= effective.get(attribute_id, frozenset())
        return frozenset(tags) if known else EVERY_TAG

    def names(self, partition: str, attribute_id: str | None) -> dict[str, str | None]:
        """`kind` or `relation` api name and the Attribute's api name (ids when unknown); for
        a schema Conflict the Attribute is its property path."""
        try:
            parsed = parse_partition(partition)
        except ValueError:
            return {"kind": None, "relation": None, "attribute": attribute_id}
        if parsed.type is PartitionType.SCHEMA:
            return {"kind": None, "relation": None, "attribute": attribute_id}
        element: str = parsed.element_id
        attribute = attribute_id
        for schema in self.schemas:
            holder = self._holder(schema, partition)
            if holder is None:
                continue
            element = holder.api_name
            if attribute_id is not None and attribute_id in holder.attributes:
                attribute = holder.attributes[attribute_id].api_name
                break
        is_kind = parsed.type is PartitionType.KIND
        return {
            "kind": element if is_kind else None,
            "relation": None if is_kind else element,
            "attribute": attribute,
        }


def conflict_payload(
    domain_id: str,
    change: ConflictChange,
    view: SchemaView,
    *,
    proposal_id: str,
    target: str,
    source: str,
    attempt_id: str,
) -> dict[str, Any]:
    """Payload of a Conflict event (module doc)."""
    row = change.conflict
    payload: dict[str, Any] = {
        "domain": domain_id,
        "branch": target,
        "source_branch": source,
        "proposal": proposal_id,
        "attempt": attempt_id,
        "conflict": row.fingerprint,
        "code": row.code,
        **view.names(row.partition, row.attribute_id),
        "key": row.element_key,
        "count": row.member_count,
    }
    if change.event_type == CONFLICT_RESOLVED:
        payload["reason"] = change.reason
        if change.reason == RESOLVED_BY_RESOLUTION:
            payload["choice"] = (row.resolution or {}).get("choice")
            payload["resolved_by"] = row.resolved_by
    payload[AUDIENCE_KEY] = audience(
        branches=(target, source), tags=view.required_tags(row.partition, row.attribute_id)
    )
    return payload


async def write_conflict_events(
    session: AsyncSession,
    domain_id: str,
    changes: Sequence[ConflictChange],
    *,
    proposal_id: str,
    target: str,
    source: str,
    attempt_id: str,
    runtime: StoreRuntime,
) -> list[int]:
    """Write `changes` to the outbox in the caller's transaction; the event ids. The schemas
    are read only when there is something to write."""
    if not changes:
        return []
    found = [await head_schema(session, domain_id, b, runtime=runtime) for b in (target, source)]
    domain = await DomainRepository(session).get(domain_id)
    reference = (
        None
        if domain is None
        else await head_schema(session, domain_id, domain.default_branch, runtime=runtime)
    )
    view = SchemaView.of(
        [None if f is None else f.schema for f in found],
        None if reference is None else reference.schema,
        runtime.schema.expression_limits,
    )
    outbox = OutboxRepository(session)
    return [
        await outbox.enqueue(
            domain_id,
            change.event_type,
            conflict_payload(
                domain_id,
                change,
                view,
                proposal_id=proposal_id,
                target=target,
                source=source,
                attempt_id=attempt_id,
            ),
        )
        for change in changes
    ]
