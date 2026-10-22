"""Schema suggestions of a Domain (design 9.2 step 10): observations of extraction jobs
aggregated by fingerprint, and the reviewer's decisions.

`record` folds a job's observations into the stored rows: per fingerprint, each document's
share (`observations[document_id] = {"n": count, "c": summed confidence}`) is replaced, so
extracting a document again does not count it twice; examples of that document are replaced,
samples, labels and sources are united, each within its bound. A decided suggestion keeps
counting observations but stays decided. New fingerprints beyond `max_tracked` rows are
dropped and counted.

Variants of one Attribute (`renewal_notice_days`, `renewal_proposal_notice_days`) are folded
into the best supported suggestion of the Kind (`merge_attributes`), their labels kept; a Kind
that only names a field or a generic word is not recorded (`kind_is_noise`) unless its evidence
shows an entity of its own (`kind_is_independent`). `merge_variants` folds variants that were
stored before they could be merged on arrival.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from sqlalchemy import Table, delete, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.infer.suggestions import (
    PROPOSED,
    AttributeSighting,
    Observation,
    SuggestionOptions,
    SuggestionType,
    kind_is_independent,
    kind_is_noise,
    merge_attributes,
)
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


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    """What `merge_variants` did: suggestions folded away and suggestions that absorbed them."""

    merged: int = 0
    kept: int = 0


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


def combine(
    kept: Mapping[Any, Any], others: Sequence[Mapping[Any, Any]], options: SuggestionOptions
) -> dict[str, Any]:
    """Column values of the suggestion `kept` after absorbing the suggestions `others` (variants
    of it): each document's share is summed, examples, samples, labels (the absorbed names
    included) and sources are united, each within its bound."""
    shares: dict[str, dict[str, float]] = {
        d: dict(v) for d, v in (kept.get("observations") or {}).items()
    }
    for other in others:
        for document, share in (other.get("observations") or {}).items():
            into = shares.setdefault(document, {"n": 0, "c": 0.0})
            into["n"] = int(into["n"]) + int(share["n"])
            into["c"] = round(float(into["c"]) + float(share["c"]), 6)
    support = sum(int(v["n"]) for v in shares.values())
    total = sum(float(v["c"]) for v in shares.values())
    every = [kept, *others]
    examples: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    for row in every:
        for example in row.get("examples") or []:
            key = (example.get("document_id"), example.get("start"), example.get("end"))
            examples.setdefault(key, example)
    return {
        "observations": shares,
        "support": support,
        "confidence": round(total / support, 4) if support else 0.0,
        "examples": list(examples.values())[: options.max_examples],
        "samples": _bounded(
            (v for r in every for v in r.get("samples") or []), options.max_samples
        ),
        "labels": _bounded(
            [
                *(kept.get("labels") or []),
                *(x for r in others for x in (r["name"], *(r.get("labels") or []))),
            ],
            options.max_labels,
        ),
        "sources": _bounded((v for r in every for v in r.get("sources") or []), MAX_SOURCES),
    }


def consolidate(
    observations: Sequence[Observation],
    stored: Sequence[Mapping[Any, Any]],
    options: SuggestionOptions,
) -> tuple[dict[str, list[Observation]], dict[str, str]]:
    """Observations grouped by the fingerprint of the suggestion they count for, and the name
    of each group. Attribute variants join the best supported suggestion of their Kind,
    among the pending ``stored`` ones and the job's; noise Kinds are left out, unless the
    evidence (the job's and the ``stored`` Kind and Attribute suggestions) shows an
    independent entity."""
    sightings: dict[str, AttributeSighting] = {}
    kinds: dict[str, Mapping[Any, Any]] = {}
    for row in stored:
        if row.get("suggestion_type", SuggestionType.ATTRIBUTE.value) == SuggestionType.KIND.value:
            kinds[row["fingerprint"]] = row
            continue
        sightings[row["fingerprint"]] = AttributeSighting(
            row["fingerprint"],
            row["name"],
            row["kind_ref"],
            tuple(row["samples"] or ()),
            int(row["support"]),
        )
    batch: dict[str, list[Observation]] = defaultdict(list)
    for observation in observations:
        batch[observation.fingerprint].append(observation)
    for key, group in batch.items():
        first = group[0]
        if first.type is not SuggestionType.ATTRIBUTE:
            continue
        known = sightings.get(key)
        samples = tuple(dict.fromkeys(o.value for o in group if o.value))
        sightings[key] = AttributeSighting(
            key,
            first.name,
            first.kind,
            tuple(dict.fromkeys([*(known.samples if known else ()), *samples])),
            (known.support if known else 0) + len(group),
        )
    merged = merge_attributes(list(sightings.values()), options)
    names = {s.key: s.name for s in sightings.values()}
    groups: dict[str, list[Observation]] = defaultdict(list)
    for key, group in batch.items():
        groups[merged.get(key, key)].extend(group)
    fields_known = [s.name for s in sightings.values()]
    for key in [k for k, g in groups.items() if g[0].type is SuggestionType.KIND]:
        name = groups[key][0].name
        owned = f"{PROPOSED}{name}"
        instances = {o.value for o in groups[key] if o.value}
        instances.update((kinds[key].get("samples") or []) if key in kinds else [])
        fields = {s.key for s in sightings.values() if s.kind == owned}
        independent = kind_is_independent(len(instances), len(fields), options)
        if kind_is_noise(name, fields_known, options, independent=independent):
            del groups[key]
    return groups, {k: names.get(k, groups[k][0].name) for k in groups}


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
        table = SUGGESTIONS
        refs = {o.kind for o in observations if o.type is SuggestionType.ATTRIBUTE and o.kind}
        kind_keys = {o.fingerprint for o in observations if o.type is SuggestionType.KIND}
        refs.update(f"{PROPOSED}{o.name}" for o in observations if o.type is SuggestionType.KIND)
        pending: list[Any] = []
        if refs:
            pending = list(
                (
                    await self._session.execute(
                        select(table).where(
                            self._scoped(),
                            table.c.suggestion_type == SuggestionType.ATTRIBUTE.value,
                            table.c.status == PENDING,
                            table.c.kind_ref.in_(sorted(refs)),
                        )
                    )
                ).mappings()
            )
        if kind_keys:
            pending += list(
                (
                    await self._session.execute(
                        select(table).where(
                            self._scoped(), table.c.fingerprint.in_(sorted(kind_keys))
                        )
                    )
                ).mappings()
            )
        groups, names = consolidate(observations, pending, options)
        if not groups:
            return RecordOutcome()
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
                        name=names[key],
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

    async def merge_variants(self, *, options: SuggestionOptions, now: datetime) -> MergeOutcome:
        """Fold the pending Attribute suggestions that are variants of one another
        (`merge_attributes`) into the best supported one of each set, as `record` does for new
        observations: support, documents, examples, samples and labels are united and the
        absorbed rows deleted. Decided suggestions are left alone."""
        table = SUGGESTIONS
        rows = {
            row["fingerprint"]: row
            for row in (
                await self._session.execute(
                    select(table)
                    .where(
                        self._scoped(),
                        table.c.suggestion_type == SuggestionType.ATTRIBUTE.value,
                        table.c.status == PENDING,
                    )
                    .order_by(table.c.id)
                    .with_for_update()
                )
            ).mappings()
        }
        sightings = [
            AttributeSighting(
                key, r["name"], r["kind_ref"], tuple(r["samples"] or ()), int(r["support"])
            )
            for key, r in rows.items()
        ]
        folded: dict[str, list[str]] = defaultdict(list)
        for key, kept in merge_attributes(sightings, options).items():
            if key != kept:
                folded[kept].append(key)
        for kept, absorbed in sorted(folded.items()):
            others = [rows[k] for k in sorted(absorbed)]
            await self._session.execute(
                update(table)
                .where(self._scoped(), table.c.id == rows[kept]["id"])
                .values(**combine(rows[kept], others, options), updated_at=now)
            )
            await self._session.execute(
                delete(table).where(self._scoped(), table.c.id.in_([o["id"] for o in others]))
            )
        return MergeOutcome(merged=sum(len(v) for v in folded.values()), kept=len(folded))

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
        kind_min_attributes: int = 0,
        limit: int,
        after: tuple[int, str] | None = None,
    ) -> list[SuggestionRow]:
        """Suggestions by support (descending), then id, after the keyset `(support, id)`;
        pending ones need `min_support`, and pending Kinds `kind_min_attributes` Attribute
        suggestions of their own (owner ref `+<Kind>`) or as many observations."""
        table = SUGGESTIONS
        query = select(table).where(
            self._scoped(), or_(table.c.status != PENDING, table.c.support >= min_support)
        )
        if kind_min_attributes > 0:
            owned = SUGGESTIONS.alias("owned")
            attributes = (
                select(func.count())
                .select_from(owned)
                .where(
                    owned.c.domain_id == table.c.domain_id,
                    owned.c.suggestion_type == SuggestionType.ATTRIBUTE.value,
                    owned.c.kind_ref == func.concat(literal(PROPOSED), table.c.name),
                )
                .scalar_subquery()
            )
            query = query.where(
                or_(
                    table.c.status != PENDING,
                    table.c.suggestion_type != SuggestionType.KIND.value,
                    table.c.support >= kind_min_attributes,
                    attributes >= kind_min_attributes,
                )
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
