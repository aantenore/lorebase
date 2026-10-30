"""Resolution misses (design 8.10): the phrases `POST /resolve` matched to nothing, counted per
Domain, branch and phrase in `resolve_misses` (revision 0046).

Keeper aggregates the misses of many requests in memory and writes them in batches off the
request path (`record_misses`); Scribe prunes the phrases not seen for
`store.resolve_misses.retention_days` (`prune_misses`) and reads the frequent ones to propose
vocabulary (`frequent_misses`).

Bounds: a Domain keeps at most `max_per_domain` phrases. Known phrases keep counting; a new
phrase beyond evicts the least missed, then least recently seen, phrase missed no more often
than it, or is dropped. A caller that alone holds `max_phrases_per_caller` phrases (its digest
the only one of each) adds no new ones. Writers of one Domain take its transaction advisory
lock (`lock_domain`), so the bounds hold across Keeper replicas. Each row keeps at most
`max_callers` caller digests (`caller_digest`), so `callers` counts distinct callers up to that
bound. The digests are HMAC-SHA256 keyed by `store.resolve_misses.caller_key`, which never
reaches the database: a reader of the table who guesses a principal id cannot confirm it. A
new key makes the stored digests incomparable, so distinct callers are counted afresh (a
caller seen under both keys counts twice until its phrases expire or are purged).

Phrases are what callers typed: they are deleted with their Domain (foreign key) and by
`purge_misses`, kept no longer than the retention period, and the vocabulary suggestions that
quote them follow (`repositories.suggestions.SchemaSuggestionRepository.sweep_vocabulary`).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import SecretStr
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


def caller_digest(domain_id: str, principal_id: str, key: SecretStr) -> str:
    """The digest counting a caller of a Domain (16 hex digits): HMAC-SHA256 under `key`
    (`store.resolve_misses.caller_key`), never the principal id."""
    digest = hmac.new(
        key.get_secret_value().encode(),
        f"lb.miss\x1f{domain_id}\x1f{principal_id}".encode(),
        hashlib.sha256,
    )
    return digest.hexdigest()[:16]


_COUNT = text("SELECT count(*) FROM resolve_misses WHERE domain_id = :domain")
_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
_ALONE = text(
    "SELECT callers[1], count(*) FROM resolve_misses WHERE domain_id = :domain "
    "AND cardinality(callers) = 1 GROUP BY callers[1]"
)
_EVICT = text(
    "DELETE FROM resolve_misses WHERE ctid = (SELECT ctid FROM resolve_misses "
    "WHERE domain_id = :domain AND misses <= :misses ORDER BY misses, last_seen LIMIT 1) "
    "RETURNING 1"
)
_LIVE = text("SELECT branch, phrase FROM resolve_misses WHERE domain_id = :domain")
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
    sightings stored (the others were beyond the Domain's or their caller's bound)."""
    by_domain: dict[str, list[MissSighting]] = {}
    for sighting in sightings:
        by_domain.setdefault(sighting.domain_id, []).append(sighting)
    stored = 0
    for domain_id, batch in sorted(by_domain.items()):
        async with rls_scope(session, domain_ids=[domain_id]):
            await lock_domain(session, domain_id)
            room = settings.max_per_domain - int(
                (await session.execute(_COUNT, {"domain": domain_id})).scalar_one()
            )
            alone = {
                str(caller): int(count)
                for caller, count in (await session.execute(_ALONE, {"domain": domain_id})).all()
            }
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
                single = sighting.callers[0] if len(set(sighting.callers)) == 1 else None
                if single is not None and alone.get(single, 0) >= settings.max_phrases_per_caller:
                    continue
                if room <= 0:
                    evicted = await session.execute(
                        _EVICT, {"domain": domain_id, "misses": sighting.misses}
                    )
                    if evicted.first() is None:
                        continue
                    room += 1
                await session.execute(_INSERT, params)
                room -= 1
                stored += 1
                if single is not None:
                    alone[single] = alone.get(single, 0) + 1
    return stored


async def lock_domain(session: AsyncSession, domain_id: str) -> None:
    """The Domain's miss lock (transaction advisory lock): writers and consolidation passes
    of one Domain take turns, so bounds and sweeps hold across processes."""
    await session.execute(_LOCK, {"key": f"resolve_misses\x1f{domain_id}"})


async def live_misses(session: AsyncSession, domain_id: str) -> set[tuple[str, str]]:
    """(branch, phrase) of every phrase the Domain keeps."""
    rows = await session.execute(_LIVE, {"domain": domain_id})
    return {(str(b), str(p)) for b, p in rows.all()}


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
    "live_misses",
    "lock_domain",
    "phrase_allowed",
    "prune_misses",
    "purge_misses",
    "record_misses",
]
