"""Reads behind reconciliation detection (design 10.6): claims from Lineage and branch history.

- `provenance_entry` turns a stored provenance row back into the core entry, and `claim_of`
  a stored row and a value into a claim. The own entry of a plain commit (an origin of type
  `import` or `operation` with only a `ref`) is attributed to its import batch
  (`import:{ref}`) or else to the commit's author (`principal:{actor}`); other entries that
  cannot be read give no claim.
- `current_claims` gives the claim of each current value of some Records on a branch: the
  value with the provenance entry its Lineage points at (vault Attributes are left out: their
  values are references, not values).
- `record_history` gives, per Record, the changes of its values along the branch: each value
  that replaced another, with the claims of both (the earlier claim is the latest provenance
  of that Attribute before the change).
- `open_counts` counts the open candidates of a Domain by type.

Nothing here writes; statements are scoped to the Domain and the branch (an overlay is read
through its resolved rows).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Origin, OriginType, ProvenanceEntry
from lorebase_core.reconcile import CandidateStatus, Claim, IdentityKind, identity
from lorebase_core.tree import kind_partition
from lorebase_core.types.encoding import format_timestamp
from lorebase_store.objectstore import PgObjectStore
from lorebase_store.repositories.materialized import MaterializedRepository, RecordRow
from lorebase_store.repositories.provenance import ProvenanceRepository, ProvenanceRow
from lorebase_store.repositories.reconciliation_candidates import CANDIDATES
from lorebase_store.settings import WriteSettings

_ORIGIN: TypeAdapter[Any] = TypeAdapter(Origin)


def provenance_entry(row: ProvenanceRow) -> ProvenanceEntry | None:
    """The core entry of a stored row; `None` when its origin document cannot be read."""
    try:
        origin = _ORIGIN.validate_python({"type": row.origin_type, **row.origin_ref})
        return ProvenanceEntry(
            id=row.id,
            origin=origin,
            actor=row.actor,
            at=format_timestamp(row.at),
            valid_from=None if row.valid_from is None else format_timestamp(row.valid_from),
            valid_to=None if row.valid_to is None else format_timestamp(row.valid_to),
        )
    except (ValidationError, ValueError):
        return None


def _plain_identity(row: ProvenanceRow) -> str | None:
    ref = row.origin_ref.get("ref")
    if row.origin_type == OriginType.IMPORT.value and isinstance(ref, str) and ref:
        return identity(IdentityKind.IMPORT, ref)
    if row.origin_type in (OriginType.IMPORT.value, OriginType.OPERATION.value):
        return identity(IdentityKind.PRINCIPAL, row.actor)
    return None


def claim_of(row: ProvenanceRow, value: Any) -> Claim | None:
    """The claim of `value` written under the provenance `row` (module doc)."""
    entry = provenance_entry(row)
    if entry is not None:
        return Claim.of_provenance(entry, value)
    source = _plain_identity(row)
    if source is None:
        return None
    return Claim(
        claim_id=row.id,
        value=value,
        source=source,
        provenance_id=row.id,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        at=row.at,
    )


async def entries(
    session: AsyncSession, domain_id: str, ids: Iterable[str], *, writes: WriteSettings
) -> dict[str, ProvenanceRow]:
    """Provenance rows by id (unknown ids are left out)."""
    return await ProvenanceRepository(session, domain_id, writes=writes).get_many(ids)


@dataclass(frozen=True, slots=True)
class CurrentValues:
    """The Records read and the claims of their current values by `(key, attribute id)`."""

    records: dict[str, RecordRow]
    claims: dict[tuple[str, str], Claim]


async def current_claims(
    repository: MaterializedRepository,
    kind_id: str,
    keys: Iterable[str],
    *,
    attributes: Collection[str],
    writes: WriteSettings,
) -> CurrentValues:
    """Claims of the current values of `attributes` of the Records `keys` (module doc). A
    value whose Lineage names no readable entry has no claim."""
    records = await repository.records(kind_id, keys)
    wanted: dict[tuple[str, str], str] = {}
    for key, row in records.items():
        for attribute_id in attributes:
            entry_id = row.lineage.get(attribute_id)
            if entry_id is not None and row.values.get(attribute_id) is not None:
                wanted[(key, attribute_id)] = entry_id
    found = await entries(
        repository.session, repository.domain_id, set(wanted.values()), writes=writes
    )
    claims: dict[tuple[str, str], Claim] = {}
    for (key, attribute_id), entry_id in wanted.items():
        stored = found.get(entry_id)
        claim = None if stored is None else claim_of(stored, records[key].values[attribute_id])
        if claim is not None:
            claims[(key, attribute_id)] = claim
    return CurrentValues(records, claims)


@dataclass(frozen=True, slots=True)
class Replacement:
    """A value of `attribute_id` that replaced another on the branch, with both claims."""

    key: str
    attribute_id: str
    before: Claim
    after: Claim


@dataclass(frozen=True, slots=True)
class RecordHistory:
    """What `record_history` read: the replacements found and how many history steps."""

    replacements: list[Replacement]
    steps: int


async def record_history(
    repository: MaterializedRepository,
    kind_id: str,
    keys: Sequence[str],
    *,
    attributes: Collection[str],
    writes: WriteSettings,
    batch: int,
) -> RecordHistory:
    """Values of `attributes` of the Records `keys` that replaced another value along the
    branch (module doc). A deletion forgets the earlier values: a Record created again starts
    a history of its own."""
    steps_by_key: dict[str, list[Any]] = defaultdict(list)
    count = 0
    async for page in repository.stream_history_steps(
        kind_partition(kind_id), batch=batch, keys=list(keys)
    ):
        for step in page:
            steps_by_key[step.element_key].append(step)
            count += 1
    hashes = {
        bytes(h)
        for steps in steps_by_key.values()
        for step in steps
        for h in (step.old_hash, step.new_hash)
        if h is not None
    }
    store = PgObjectStore(repository.session, repository.domain_id, writes=writes, batch=batch)
    bodies = {
        digest: stored.body.get("values") or {}
        for digest, stored in (await store.get_many(hashes)).items()
    }
    ids = {
        entry_id
        for steps in steps_by_key.values()
        for step in steps
        for attribute_id, entry_id in (step.provenance or {}).items()
        if attribute_id in attributes
    }
    found = await entries(repository.session, repository.domain_id, ids, writes=writes)
    replacements: list[Replacement] = []
    for key, steps in steps_by_key.items():
        latest: dict[str, Claim] = {}
        for step in steps:
            if step.new_hash is None:
                latest.clear()
                continue
            values: Mapping[str, Any] = bodies.get(bytes(step.new_hash), {})
            for attribute_id, entry_id in (step.provenance or {}).items():
                if attribute_id not in attributes:
                    continue
                stored = found.get(entry_id)
                value = values.get(attribute_id)
                claim = None if stored is None or value is None else claim_of(stored, value)
                if claim is None:
                    latest.pop(attribute_id, None)
                    continue
                before = latest.get(attribute_id)
                if before is not None and before.value_hash != claim.value_hash:
                    replacements.append(Replacement(key, attribute_id, before, claim))
                latest[attribute_id] = claim
    return RecordHistory(replacements, count)


async def open_counts(session: AsyncSession, domain_id: str) -> dict[str, int]:
    """Open candidates of a Domain by type."""
    result = await session.execute(
        select(CANDIDATES.c.type, func.count())
        .where(
            CANDIDATES.c.domain_id == domain_id,
            CANDIDATES.c.status == CandidateStatus.OPEN.value,
        )
        .group_by(CANDIDATES.c.type)
    )
    return {str(kind): int(count) for kind, count in result.all()}


__all__ = [
    "CurrentValues",
    "RecordHistory",
    "Replacement",
    "claim_of",
    "current_claims",
    "entries",
    "open_counts",
    "provenance_entry",
    "record_history",
]
