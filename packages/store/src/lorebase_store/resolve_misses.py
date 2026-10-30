"""Resolution misses (design 8.10): the phrases `POST /resolve` matched to nothing, counted per
Domain, branch and phrase in `resolve_misses` (revision 0046).

Keeper aggregates the misses of many requests in memory and writes them in batches off the
request path (`record_misses`); Scribe prunes the phrases not seen for
`store.resolve_misses.retention_days` (`prune_misses`) and reads the frequent ones to propose
vocabulary (`frequent_misses`). A Domain keeps at most `max_per_domain` phrases: known phrases
keep counting, new ones beyond are dropped until pruning makes room. Each row keeps at most
`max_callers` caller digests (`caller_digest`), so `callers` counts distinct callers up to
that bound; the digests name nobody, though an administrator who knows a principal id can
recompute its digest, so they stay in the database like the phrases.

Phrases are what callers typed: they are deleted with their Domain (foreign key) and by
`purge_misses`, and kept no longer than the retention period.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.auth.scope import rls_scope
from lorebase_store.settings import ResolveMissSettings


@dataclass(frozen=True, slots=True)
class MissSighting:
    """Misses of one phrase, aggregated over some requests: how many, by which callers
    (digests), first and last seen."""

    domain_id: str
    branch: str
    phrase: str
    misses: int
    callers: tuple[str, ...]
    first_seen: datetime
    last_seen: datetime


@dataclass(frozen=True, slots=True)
class MissRow:
    """A stored phrase: `callers` is the number of distinct callers kept."""

    branch: str
    phrase: str
    misses: int
    callers: int
    first_seen: datetime
    last_seen: datetime


def caller_digest(domain_id: str, principal_id: str) -> str:
    """The digest counting a caller of a Domain (16 hex digits); never the principal id."""
    digest = hashlib.blake2b(
        f"{domain_id}\x1f{principal_id}".encode(), digest_size=8, person=b"lb.miss"
    )
    return digest.hexdigest()


_COUNT = text("SELECT count(*) FROM resolve_misses WHERE domain_id = :domain")
_CALLERS = (
    "CASE WHEN cardinality(m.callers) >= :max_callers THEN m.callers ELSE ("
    "SELECT coalesce(array_agg(u.c ORDER BY u.o), '{}'::text[]) FROM ("
    "SELECT c, min(o) AS o FROM unnest(m.callers || CAST(:callers AS text[])) "
    "WITH ORDINALITY AS x(c, o) GROUP BY c ORDER BY min(o) LIMIT :max_callers) AS u) END"
)
_UPDATE = text(
    "UPDATE resolve_misses AS m SET misses = m.misses + :misses, "
    "first_seen = LEAST(m.first_seen, :first), last_seen = GREATEST(m.last_seen, :last), "
    f"callers = {_CALLERS} "
    "WHERE m.domain_id = :domain AND m.branch = :branch AND m.phrase = :phrase RETURNING 1"
)
_INSERT = text(
    "INSERT INTO resolve_misses AS m (domain_id, branch, phrase, misses, callers, first_seen, "
    "last_seen) VALUES (:domain, :branch, :phrase, :misses, "
    "(SELECT coalesce(array_agg(c), '{}'::text[]) FROM (SELECT DISTINCT c FROM "
    "unnest(CAST(:callers AS text[])) AS x(c) ORDER BY c LIMIT :max_callers) AS u), "
    ":first, :last) "
    "ON CONFLICT (domain_id, branch, phrase) DO UPDATE SET misses = m.misses + EXCLUDED.misses, "
    "first_seen = LEAST(m.first_seen, EXCLUDED.first_seen), "
    f"last_seen = GREATEST(m.last_seen, EXCLUDED.last_seen), callers = {_CALLERS}"
)
_PRUNE = text(
    "DELETE FROM resolve_misses WHERE ctid = ANY(ARRAY(SELECT ctid FROM resolve_misses "
    "WHERE domain_id = :domain AND last_seen < :before LIMIT :batch))"
)
_PURGE = text("DELETE FROM resolve_misses WHERE domain_id = :domain")
_FREQUENT = text(
    "SELECT branch, phrase, misses, cardinality(callers), first_seen, last_seen "
    "FROM resolve_misses WHERE domain_id = :domain AND branch = :branch "
    "AND misses >= :min_misses AND cardinality(callers) >= :min_callers "
    "ORDER BY misses DESC, phrase LIMIT :limit"
)


async def record_misses(
    session: AsyncSession, sightings: Iterable[MissSighting], *, settings: ResolveMissSettings
) -> int:
    """Add `sightings` to the stored counts, in the caller's transaction; the number of
    sightings stored (the others were beyond `max_per_domain`)."""
    by_domain: dict[str, list[MissSighting]] = {}
    for sighting in sightings:
        by_domain.setdefault(sighting.domain_id, []).append(sighting)
    stored = 0
    for domain_id, batch in sorted(by_domain.items()):
        async with rls_scope(session, domain_ids=[domain_id]):
            room = settings.max_per_domain - int(
                (await session.execute(_COUNT, {"domain": domain_id})).scalar_one()
            )
            for sighting in sorted(batch, key=lambda s: (s.branch, s.phrase)):
                params = {
                    "domain": domain_id,
                    "branch": sighting.branch,
                    "phrase": sighting.phrase,
                    "misses": sighting.misses,
                    "callers": list(sighting.callers),
                    "first": sighting.first_seen,
                    "last": sighting.last_seen,
                    "max_callers": settings.max_callers,
                }
                if (await session.execute(_UPDATE, params)).first() is not None:
                    stored += 1
                    continue
                if room <= 0:
                    continue
                await session.execute(_INSERT, params)
                room -= 1
                stored += 1
    return stored


async def prune_misses(
    session: AsyncSession, domain_id: str, before: datetime, *, batch: int
) -> int:
    """Delete the Domain's phrases last seen before `before`, `batch` at a time; the number
    deleted. Runs in the caller's transaction and row-level security scope."""
    deleted = 0
    while True:
        result = await session.execute(
            _PRUNE, {"domain": domain_id, "before": before, "batch": batch}
        )
        count = int(getattr(result, "rowcount", 0) or 0)
        deleted += count
        if count < batch:
            return deleted


async def purge_misses(session: AsyncSession, domain_id: str) -> int:
    """Delete every phrase of the Domain; the number deleted."""
    result = await session.execute(_PURGE, {"domain": domain_id})
    return int(getattr(result, "rowcount", 0) or 0)


async def frequent_misses(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    *,
    min_misses: int,
    min_callers: int,
    limit: int,
) -> list[MissRow]:
    """The phrases of `branch` missed at least `min_misses` times by at least `min_callers`
    distinct callers, the most missed first, at most `limit`."""
    rows = await session.execute(
        _FREQUENT,
        {
            "domain": domain_id,
            "branch": branch,
            "min_misses": min_misses,
            "min_callers": min_callers,
            "limit": limit,
        },
    )
    return [
        MissRow(str(b), str(p), int(n), int(c), first, last)
        for b, p, n, c, first, last in rows.all()
    ]


def phrase_allowed(phrase: str, settings: ResolveMissSettings, ignored: Sequence[str]) -> bool:
    """Whether a phrase (compared form) may be recorded: within the length bounds and not one
    of the `ignored` words (compared forms of `ignore_words`)."""
    return (
        settings.min_phrase_chars <= len(phrase) <= settings.max_phrase_chars
        and phrase not in ignored
    )


__all__ = [
    "MissRow",
    "MissSighting",
    "caller_digest",
    "frequent_misses",
    "phrase_allowed",
    "prune_misses",
    "purge_misses",
    "record_misses",
]
