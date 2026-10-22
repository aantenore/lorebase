"""Side effects of stored Operations (design 5.7, 10.5): outbox events of a committed execution.

An Operation names its side effects (`side_effects: [risk_flag_raised]`). A committed execution
writes one `operation.<name>` event per name to the outbox, in the transaction of its last
Revision, its invocation record and its audit entry: the events exist if and only if the
execution committed. Dry runs, refused executions, executions sent to review (agent writes on a
task branch whose Proposal reviews them included) and replays of an idempotency key write none.
Consumers of the outbox (the webhook worker) deliver them later.

The payload names the execution (Operation, invocation, principal, branch, Revisions, head),
carries the parameters as recorded in the invocation (`recorded_parameters`: values that may be
sensitive are `***` for every subscriber) and up to `side_effect_max_edits` edits. Values of
sensitive Attributes in the edits are wrapped with their effective tags
(`lorebase_store.outbox_events.sensitive_value`), and values of vault Attributes are their
vault references as committed, never the personal data: the webhook worker shows them only to
webhooks cleared for those tags. A parameter or value whose JSON holds `$sensitive` or `$vault`
keys and is not a committed vault reference is wrapped with every tag, so caller-written JSON
cannot pose as a wrapper or open a vault value for a webhook not cleared for everything.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.canonical import hex
from lorebase_core.model import Attribute, Operation, Schema, Sensitivity, looks_like_reference
from lorebase_core.validate import DataChange, RecordUpsert
from lorebase_keeper.reading.sensitivity import Clearance, Masking
from lorebase_keeper.services.operation_catalog import edit_document
from lorebase_store.outbox_events import carries_markers, operation_event_type, sensitive_value
from lorebase_store.repositories import MaterializedRepository
from lorebase_store.repositories.events import OutboxRepository
from lorebase_store.telemetry import OPERATION_EVENTS


@dataclass(frozen=True, slots=True)
class _Wrapping(Masking):
    """Keeps every value, wrapping each one that needs a clearance with its tags; a value of an
    Attribute the schema does not know needs every clearance."""

    def values(
        self,
        attributes: Mapping[str, Attribute],
        values: Mapping[str, Any],
        kinds: Sequence[str],
        derived: Mapping[str, Any] | None = None,
        *,
        omit: bool | None = None,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for attribute_id, attribute in attributes.items():
            source = derived if attribute.is_derived and derived is not None else values
            if attribute_id in source:
                out[attribute.api_name] = self.value(attribute, kinds, source[attribute_id])
        return out

    def value(self, attribute: Attribute | None, kinds: Sequence[str], value: Any) -> Any:
        if value is None:
            return None
        committed_reference = (
            attribute is not None and attribute.in_vault and looks_like_reference(value)
        )
        if attribute is None or (carries_markers(value) and not committed_reference):
            return sensitive_value(frozenset(Sensitivity), value)
        tags = self.tags(attribute)
        return sensitive_value(tags, value) if tags else value


def guarded(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Recorded parameters, each whose JSON holds `$sensitive` or `$vault` keys wrapped with
    every tag (see module doc)."""
    return {
        name: sensitive_value(frozenset(Sensitivity), value) if carries_markers(value) else value
        for name, value in parameters.items()
    }


def wrapping(masking: Masking) -> Masking:
    """A masking that wraps sensitive values with the tags `masking` gives them."""
    return _Wrapping(Clearance(), masking.settings, masking.reference, masking.effective)


async def _committed(
    schema: Schema, changes: Sequence[DataChange], rows: MaterializedRepository
) -> list[DataChange]:
    """`changes` with the values of vault Attributes as committed (vault references); a value
    without a reference is left out, so no plain personal data reaches the outbox."""
    keys: dict[str, list[str]] = defaultdict(list)
    for change in changes:
        if isinstance(change, RecordUpsert):
            kind = schema.kinds.get(change.record.kind)
            if kind is not None and any(a.in_vault for a in kind.attributes.values()):
                keys[kind.id].append(change.record.key)
    if not keys:
        return list(changes)
    stored = {kind_id: await rows.records(kind_id, found) for kind_id, found in keys.items()}
    out: list[DataChange] = []
    for change in changes:
        if not isinstance(change, RecordUpsert) or change.record.kind not in stored:
            out.append(change)
            continue
        record = change.record
        kind = schema.kinds[record.kind]
        row = stored[record.kind].get(record.key)
        values = dict(record.values)
        for attribute in kind.attributes.values():
            if not attribute.in_vault or attribute.id not in values:
                continue
            held = None if row is None else row.values.get(attribute.id)
            if looks_like_reference(held):
                values[attribute.id] = held
            else:
                del values[attribute.id]
        out.append(RecordUpsert(record=record.model_copy(update={"values": values})))
    return out


async def write_side_effects(
    session: AsyncSession,
    domain_id: str,
    operation: Operation,
    *,
    schema: Schema,
    masking: Masking,
    rows: MaterializedRepository,
    changes: Sequence[DataChange],
    execution: Mapping[str, Any],
    max_edits: int,
) -> list[int]:
    """Write the outbox events of a committed execution of `operation` in the caller's
    transaction; the event ids. `execution` holds the fields every event carries (branch,
    invocation, principal, Revisions, recorded parameters)."""
    if not operation.side_effects:
        return []
    listed = await _committed(schema, changes[:max_edits], rows)
    shown = wrapping(masking)
    body: dict[str, Any] = {
        "domain": domain_id,
        **execution,
        "operation": operation.api_name,
        "operation_id": operation.id,
        "edit_count": len(changes),
        "edits": [edit_document(schema, change, shown) for change in listed],
        "edits_truncated": len(changes) > max_edits,
    }
    outbox = OutboxRepository(session)
    ids = [
        await outbox.enqueue(domain_id, operation_event_type(name), {**body, "side_effect": name})
        for name in operation.side_effects
    ]
    return ids


def counted(ids: Sequence[int]) -> None:
    """Count events whose transaction committed."""
    if ids:
        OPERATION_EVENTS.add(len(ids))


def execution_fields(
    *,
    branch: str,
    invocation_id: str,
    principal: str,
    revisions: Sequence[bytes],
    head: bytes,
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """The execution fields of an event payload."""
    return {
        "branch": branch,
        "invocation_id": invocation_id,
        "principal": principal,
        "revisions": [hex(r) for r in revisions],
        "head": hex(head),
        "parameters": guarded(parameters),
    }
