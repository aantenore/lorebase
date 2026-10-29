"""The Source sync trigger of reconciliation (design 10.6, trigger 2; 9.1 step 5).

After a run committed its changes to the Source branch and before the merge policy is applied,
each value the run wrote is compared with the value the target branch holds, through the claim
of each: the run's provenance entry (with the period the mapping read) and the provenance the
target's Lineage points at (the value the merge would replace).

- Different source identities: stages b and c decide (`contradiction` or `superseded`).
- The same identity with another value: the target holds an older value of this Source and the
  newer one waits in the Proposal. When the merge policy leaves it waiting (`proposal`), a
  `stale` candidate names both.
- Records the run added are checked for duplicates on the Source branch: equal folded unique or
  title values, and similar titles when the run added at most `max_pairs_per_kind` Records (one
  statement each).

Candidates are raised on the target branch; the caller links them to the Proposal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Kind, ProvenanceEntry
from lorebase_core.reconcile import Claim, Stage, stale_verdict
from lorebase_core.validate import RecordUpsert
from lorebase_scribe.reconcile.detector import Detector, Raised, compared
from lorebase_scribe.reconcile.duplicates import normalized_pairs, trigram_pairs
from lorebase_scribe.sources.delta import Delta
from lorebase_store.reconciliation import current_claims
from lorebase_store.repositories import MaterializedRepository
from lorebase_store.runtime import StoreRuntime


@dataclass(frozen=True, slots=True)
class _Written:
    key: str
    attribute_id: str
    claim: Claim


def _written(delta: Delta, kind: Kind, owned: frozenset[str]) -> list[_Written]:
    found: list[_Written] = []
    for group in delta.groups:
        for change in group.changes:
            if not isinstance(change.change, RecordUpsert) or change.provenance is None:
                continue
            record = change.change.record
            for attribute_id, value in record.values.items():
                if attribute_id not in owned or attribute_id in kind.primary_key:
                    continue
                if value is None or not compared(kind.attributes.get(attribute_id)):
                    continue
                entry: ProvenanceEntry = change.periods.get(attribute_id, change.provenance)
                found.append(_Written(record.key, attribute_id, Claim.of_provenance(entry, value)))
    return found


async def sync_disagreements(
    detector: Detector,
    session: AsyncSession,
    runtime: StoreRuntime,
    kind: Kind,
    delta: Delta,
    *,
    owned: frozenset[str],
    domain_id: str,
    branch: str,
    target: str,
    waits: bool,
) -> list[Raised]:
    """The candidates of a run's changes (module doc). `branch` is the Source branch, `target`
    the branch the Proposal merges into, `waits` whether the merge policy leaves the changes
    waiting for review."""
    writes = runtime.settings.writes
    raised: list[Raised] = []
    written = _written(delta, kind, owned)
    if written and detector.runs(Stage.VALUES):
        target_rows = MaterializedRepository(session, domain_id, target, writes=writes)
        current = await current_claims(
            target_rows,
            kind.id,
            {w.key for w in written},
            attributes={w.attribute_id for w in written},
            writes=writes,
        )
        for item in written:
            before = current.claims.get((item.key, item.attribute_id))
            attribute = kind.attributes[item.attribute_id]
            if before is None or before.value_hash == item.claim.value_hash:
                continue
            if before.source is not None and before.source == item.claim.source:
                if waits:
                    verdict = stale_verdict(
                        before,
                        branch=target,
                        kind_id=kind.id,
                        key=item.key,
                        attribute_id=item.attribute_id,
                        now=item.claim.written_at,
                        max_age=None,
                        pending=[item.claim],
                    )
                    detector.collect(verdict, attribute, raised)
                continue
            verdict = detector.verdict(
                [before, item.claim], branch=target, kind=kind, key=item.key, attribute=attribute
            )
            detector.collect(verdict, attribute, raised)
    if delta.added and detector.runs(Stage.IDENTITY):
        raised.extend(
            await _duplicates(
                detector, session, runtime, kind, delta.added, domain_id, branch, target
            )
        )
    return raised


async def _duplicates(
    detector: Detector,
    session: AsyncSession,
    runtime: StoreRuntime,
    kind: Kind,
    added: Sequence[str],
    domain_id: str,
    branch: str,
    target: str,
) -> list[Raised]:
    options = detector.options.duplicates
    rows = MaterializedRepository(session, domain_id, branch, writes=runtime.settings.writes)
    raised = await normalized_pairs(
        session,
        await rows.layout(),
        kind,
        branch=target,
        limit=options.max_pairs_per_kind,
        involving=frozenset(added),
    )
    if kind.title_attribute is not None and len(added) <= options.max_pairs_per_kind:
        records = await rows.records(kind.id, added)
        similar, _statements = await trigram_pairs(
            session,
            runtime,
            kind,
            ((key, row.title) for key, row in sorted(records.items())),
            domain_id=domain_id,
            read_branch=branch,
            branch=target,
            threshold=options.trigram_threshold,
            limit=max(options.max_pairs_per_kind - len(raised), 0),
        )
        known = {r.draft.fingerprint for r in raised}
        raised.extend(r for r in similar if r.draft.fingerprint not in known)
    return raised


__all__ = ["sync_disagreements"]
