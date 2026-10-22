"""Schema suggestions of a Domain (design 9.2 step 10): observations of extraction jobs
aggregated by fingerprint, and the reviewer's decisions.

`record` folds a job's observations into the stored rows: per fingerprint, each document's
share (`observations[document_id] = {"n": count, "c": summed confidence}`) is replaced, so
extracting a document again does not count it twice; examples of that document are replaced,
samples, labels and sources are united, each within its bound. A decided suggestion keeps
counting observations but stays decided. New fingerprints beyond `max_tracked` rows are
dropped and counted.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from sqlalchemy import Table, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.infer.suggestions import Observation, SuggestionOptions
from lorebase_store.models import metadata

SUGGESTIONS: Table = metadata.tables["schema_suggestions"]

PENDING = "pending"
ACCEPTED = "accepted"
REJECTED = "rejected"
STATUSES = (PENDING, ACCEPTED, REJECTED)
MAX_SOURCES = 10


@dataclass(frozen=True, slots=True)
class SuggestionRow:
    id: str
    fingerprint: str
    suggestion_type: str
    name: str
    kind_ref: str | None
    target_ref: str | None
    labels: list[str]
    support: int
    confidence: float
    observations: dict[str, Any]
    examples: list[dict[str, Any]]
    samples: list[str]
    sources: list[str]
    status: str
    decided_by: str | None
    decided_at: datetime | None
    proposal_id: str | None
    branch: str | None
    last_job_id: str | None
    created_at: datetime | None
    updated_at: datetime | None

    @property
    def documents(self) -> int:
        return len(self.observations)


_FIELDS = tuple(f.name for f in fields(SuggestionRow))


def _row(found: Any) -> SuggestionRow:
    return SuggestionRow(**{name: found[name] for name in _FIELDS})


@dataclass(frozen=True, slots=True)
class RecordOutcome:
    created: int = 0
    updated: int = 0
    dropped: int = 0


def _bounded(items: Iterable[Any], limit: int) -> list[Any]:
    return list(dict.fromkeys(items))[:limit]


def fold(
    current: Mapping[Any, Any] | None,
    observations: Sequence[Observation],
    options: SuggestionOptions,
) -> dict[str, Any]:
    """Column values of a suggestion after a job's `observations` of it (one fingerprint)."""
    shares: dict[str, dict[str, float]] = dict((current or {}).get("observations") or {})
    replaced = {o.document_id for o in observations}
    batch: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "c": 0.0})
    for observation in observations:
        share = batch[observation.document_id]
        share["n"] += 1
        share["c"] = round(share["c"] + observation.confidence, 6)
    shares.update({k: {"n": int(v["n"]), "c": v["c"]} for k, v in batch.items()})
    support = sum(int(v["n"]) for v in shares.values())
    total = sum(float(v["c"]) for v in shares.values())
    kept = [
        e for e in ((current or {}).get("examples") or []) if e.get("document_id") not in replaced
    ]
    seen = {(e.get("document_id"), e.get("start"), e.get("end")) for e in kept}
    fresh: list[dict[str, Any]] = []
    for observation in observations:
        evidence = observation.evidence
        if evidence is None:
            continue
        key = (evidence.get("document_id"), evidence.get("start"), evidence.get("end"))
        if key not in seen:
            seen.add(key)
            fresh.append(dict(evidence))
    # Examples from documents not shown yet come first, so they span documents.
    shown = {e.get("document_id") for e in kept}
    fresh.sort(key=lambda e: e.get("document_id") in shown)
    examples = (kept + fresh)[: options.max_examples]
    samples = _bounded(
        [
            *((current or {}).get("samples") or []),
            *(o.value for o in observations if o.value),
        ],
        options.max_samples,
    )
    labels = _bounded(
        [*((current or {}).get("labels") or []), *(o.label for o in observations)],
        options.max_labels,
    )
    sources = _bounded(
        [*((current or {}).get("sources") or []), *(o.source for o in observations)], MAX_SOURCES
    )
    return {
        "observations": shares,
        "support": support,
        "confidence": round(total / support, 4) if support else 0.0,
        "examples": examples,
        "samples": samples,
        "labels": labels,
        "sources": sources,
    }


class SchemaSuggestionRepository:
    """Schema suggestions of one Domain, in the caller's transaction (after `scope_to_domain`)."""

    def __init__(self, session: AsyncSession, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id

    def _scoped(self) -> Any:
        return SUGGESTIONS.c.domain_id == self._domain

    async def record(
        self,
        observations: Sequence[Observation],
        *,
        job_id: str,
        options: SuggestionOptions,
        now: datetime,
        new_id: Callable[[], str],
    ) -> RecordOutcome:
        """Fold a job's observations into the stored suggestions (see module doc)."""
        groups: dict[str, list[Observation]] = defaultdict(list)
        for observation in observations:
            groups[observation.fingerprint].append(observation)
        if not groups:
            return RecordOutcome()
        table = SUGGESTIONS
        stored = {
            row["fingerprint"]: row
            for row in (
                await self._session.execute(
                    select(table)
                    .where(self._scoped(), table.c.fingerprint.in_(list(groups)))
                    .with_for_update()
                )
            ).mappings()
        }
        room = options.max_tracked - await self.count()
        created = updated = dropped = 0
        for key in sorted(groups):
            group = groups[key]
            current = stored.get(key)
            values = fold(current, group, options)
            if current is not None:
                await self._session.execute(
                    update(table)
                    .where(self._scoped(), table.c.id == current["id"])
                    .values(**values, last_job_id=job_id, updated_at=now)
                )
                updated += 1
                continue
            if room <= 0:
                dropped += len(group)
                continue
            first = group[0]
            inserted = (
                await self._session.execute(
                    pg_insert(table)
                    .values(
                        domain_id=self._domain,
                        id=new_id(),
                        fingerprint=key,
                        suggestion_type=first.type.value,
                        name=first.name,
                        kind_ref=first.kind,
                        target_ref=first.target,
                        last_job_id=job_id,
                        created_at=now,
                        updated_at=now,
                        **values,
                    )
                    .on_conflict_do_nothing(index_elements=["domain_id", "fingerprint"])
                    .returning(table.c.id)
                )
            ).scalar_one_or_none()
            if inserted is None:  # another job inserted it meanwhile: fold into its row
                row = (
                    (
                        await self._session.execute(
                            select(table)
                            .where(self._scoped(), table.c.fingerprint == key)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one()
                )
                await self._session.execute(
                    update(table)
                    .where(self._scoped(), table.c.id == row["id"])
                    .values(**fold(row, group, options), last_job_id=job_id, updated_at=now)
                )
                updated += 1
                continue
            room -= 1
            created += 1
        return RecordOutcome(created, updated, dropped)

    async def count(self, status: str | None = None) -> int:
        query = select(func.count()).select_from(SUGGESTIONS).where(self._scoped())
        if status is not None:
            query = query.where(SUGGESTIONS.c.status == status)
        return int((await self._session.execute(query)).scalar_one())

    async def page(
        self,
        *,
        statuses: Sequence[str] = (),
        types: Sequence[str] = (),
        min_support: int = 1,
        limit: int,
        after: tuple[int, str] | None = None,
    ) -> list[SuggestionRow]:
        """Suggestions by support (descending), then id, after the keyset `(support, id)`;
        pending ones need `min_support`."""
        table = SUGGESTIONS
        query = select(table).where(
            self._scoped(), or_(table.c.status != PENDING, table.c.support >= min_support)
        )
        if statuses:
            query = query.where(table.c.status.in_(list(statuses)))
        if types:
            query = query.where(table.c.suggestion_type.in_(list(types)))
        if after is not None:
            support, last = after
            query = query.where(
                or_(table.c.support < support, (table.c.support == support) & (table.c.id > last))
            )
        query = query.order_by(table.c.support.desc(), table.c.id).limit(limit)
        return [_row(found) for found in (await self._session.execute(query)).mappings()]

    async def get_many(
        self, ids: Sequence[str], *, for_update: bool = False
    ) -> dict[str, SuggestionRow]:
        if not ids:
            return {}
        query = select(SUGGESTIONS).where(self._scoped(), SUGGESTIONS.c.id.in_(list(ids)))
        if for_update:
            query = query.with_for_update()
        rows = (await self._session.execute(query)).mappings()
        return {row["id"]: _row(row) for row in rows}

    async def decide(
        self,
        ids: Sequence[str],
        *,
        status: str,
        decided_by: str | None,
        decided_at: datetime | None,
        proposal_id: str | None = None,
        branch: str | None = None,
    ) -> None:
        if not ids:
            return
        await self._session.execute(
            update(SUGGESTIONS)
            .where(self._scoped(), SUGGESTIONS.c.id.in_(list(ids)))
            .values(
                status=status,
                decided_by=decided_by,
                decided_at=decided_at,
                proposal_id=proposal_id,
                branch=branch,
                updated_at=func.now() if decided_at is None else decided_at,
            )
        )
