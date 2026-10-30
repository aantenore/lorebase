"""Coverage statistics of the default branch head (design 8.11): what reads report about how
complete an answer can be.

`mat_coverage` holds, per Kind of the default branch head, the live Records and, per stored
Attribute, how many of them miss a value: the Attribute absent, JSON null, an empty string or
an empty list (`MISSING`). For each incremental Source with `stale_after` that writes the
Kind, it also holds how many of the Records it owns no run has seen for that long
(`source_written`, design 9.1). `run_coverage` recounts a Domain when its default branch head
moved since the stored statistics (or with `force`), one statement per Kind under
`store.coverage.statement_timeout_ms`; a Kind of more than `max_records_per_kind` Records is
counted over that many of them and its missing counts scaled to the whole Kind (`sampled`).
Reads (`read_coverage`, `last_runs`) are primary key lookups, so a read pays two small
statements however many Records the Kind holds.

The statistics are counted over every row of the Kind, values of sensitive Attributes
included: readers decide what a caller may see (Kinds in its scope, Attributes it may read
values of). They describe the head they were counted at (`revision`); readers mark them
approximate once the head moved. Branches other than the default one, and older Revisions,
have none.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import Kind, Schema, SyncMode
from lorebase_store.embeddings import branch_schema
from lorebase_store.repositories.scope import scope_to_domain
from lorebase_store.settings import CoverageSettings

MISSING = (
    '(m."values" -> :{name}) IS NULL '
    "OR (m.\"values\" -> :{name}) IN ('null'::jsonb, '\"\"'::jsonb, '[]'::jsonb)"
)
"""When a stored value counts as missing (one bind parameter per Attribute id)."""

_DEFAULT_HEAD = text(
    "SELECT d.default_branch, r.head FROM domains d LEFT JOIN refs r "
    "ON r.domain_id = d.id AND r.name = d.default_branch AND r.deleted_at IS NULL "
    "WHERE d.id = :domain"
)
_STORED = text(
    "SELECT kind_id, revision FROM mat_coverage WHERE domain_id = :domain AND branch = :branch"
)
_READ = text(
    "SELECT kind_id, revision, computed_at, records, sampled, missing, unseen "
    "FROM mat_coverage WHERE domain_id = :domain AND branch = :branch "
    "AND kind_id = ANY(CAST(:kinds AS text[]))"
)
_RECORDS = text(
    "SELECT count(*) FROM mat_records m WHERE m.domain_id = :domain AND m.branch = :branch "
    "AND m.kind_id = :kind AND NOT m.deleted"
)
_UNSEEN = text(
    "SELECT count(*) FROM source_written w WHERE w.domain_id = :domain "
    "AND w.source_id = :source AND w.target_branch = :branch AND w.element = :kind "
    "AND NOT w.deleted AND coalesce(w.last_seen_at, w.updated_at) < :before"
)
_SAVE = text(
    "INSERT INTO mat_coverage (domain_id, branch, kind_id, revision, computed_at, records, "
    "sampled, missing, unseen) VALUES (:domain, :branch, :kind, :revision, :at, :records, "
    ":sampled, :missing, :unseen) ON CONFLICT (domain_id, branch, kind_id) DO UPDATE SET "
    "revision = EXCLUDED.revision, computed_at = EXCLUDED.computed_at, "
    "records = EXCLUDED.records, sampled = EXCLUDED.sampled, missing = EXCLUDED.missing, "
    "unseen = EXCLUDED.unseen"
).bindparams(bindparam("missing", type_=JSONB), bindparam("unseen", type_=JSONB))
_DROP_OTHERS = text(
    "DELETE FROM mat_coverage WHERE domain_id = :domain "
    "AND (branch <> :branch OR NOT (kind_id = ANY(CAST(:kinds AS text[]))))"
)
_LAST_RUNS = text(
    "SELECT s.id, (SELECT r.finished_at FROM source_runs r WHERE r.domain_id = :domain "
    "AND r.source_id = s.id AND r.status = 'succeeded' "
    "AND coalesce(r.stats ->> 'dry_run', 'false') <> 'true' "
    "ORDER BY r.started_at DESC NULLS LAST LIMIT 1) "
    "FROM unnest(CAST(:sources AS text[])) AS s(id)"
)
_TIMEOUT = text("SELECT set_config('statement_timeout', :timeout, true)")


@dataclass(frozen=True, slots=True)
class KindCoverage:
    """The statistics of one Kind (module doc); `missing` and `unseen` by Attribute and
    Source id."""

    kind_id: str
    revision: bytes
    computed_at: datetime
    records: int
    sampled: bool
    missing: Mapping[str, int]
    unseen: Mapping[str, int]


@dataclass(slots=True)
class CoverageReport:
    """What a pass did: Kinds counted, Kinds whose count failed (kept as they were)."""

    domain_id: str
    skipped: str | None = None
    counted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain_id,
            "skipped": self.skipped,
            "counted": len(self.counted),
            "failed": list(self.failed),
        }


def _attributes(kind: Kind) -> list[str]:
    """Ids of the Attributes whose values rows store (derived ones are computed)."""
    return sorted(a.id for a in kind.attributes.values() if not a.is_derived)


def _stats_statement(attributes: Sequence[str]) -> Any:
    columns = ", ".join(
        f"count(*) FILTER (WHERE {MISSING.format(name=f'a{i}')})" for i in range(len(attributes))
    )
    return text(
        f'SELECT count(*){", " if columns else ""}{columns} FROM (SELECT m."values" '
        "FROM mat_records m WHERE m.domain_id = :domain AND m.branch = :branch "
        "AND m.kind_id = :kind AND NOT m.deleted LIMIT :cap) AS m"
    )


async def _count_kind(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    kind: Kind,
    schema: Schema,
    settings: CoverageSettings,
    now: datetime,
) -> tuple[int, bool, dict[str, int], dict[str, int]]:
    attributes = _attributes(kind)
    params = {"domain": domain_id, "branch": branch, "kind": kind.id}
    records = int((await session.execute(_RECORDS, params)).scalar_one())
    cap = settings.max_records_per_kind
    row = (
        await session.execute(
            _stats_statement(attributes),
            {**params, "cap": cap, **{f"a{i}": a for i, a in enumerate(attributes)}},
        )
    ).one()
    counted = int(row[0])
    sampled = records > counted
    scale = records / counted if sampled and counted else 1.0
    missing = {a: round(int(row[i + 1]) * scale) for i, a in enumerate(attributes)}
    unseen: dict[str, int] = {}
    for source in schema.sources.values():
        delta = source.stale_after_delta
        if source.target_kind != kind.id or source.sync_mode is not SyncMode.INCREMENTAL:
            continue
        if delta is None:
            continue
        found = await session.execute(
            _UNSEEN, {**params, "source": source.id, "before": now - delta}
        )
        unseen[source.id] = int(found.scalar_one())
    return records, sampled, missing, unseen


async def run_coverage(
    sessions: async_sessionmaker[AsyncSession],
    domain_id: str,
    *,
    settings: CoverageSettings,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    force: bool = False,
) -> CoverageReport:
    """Recount the statistics of the Domain's default branch head when it moved since they
    were counted (or with `force`); each Kind in its own transaction, so a Kind whose count
    times out keeps its previous statistics and the others are still counted."""
    report = CoverageReport(domain_id)
    if not settings.enabled:
        report.skipped = "disabled"
        return report
    async with sessions() as session, session.begin():
        await scope_to_domain(session, domain_id)
        found = (await session.execute(_DEFAULT_HEAD, {"domain": domain_id})).one_or_none()
        if found is None or found[1] is None:
            report.skipped = "no default branch head"
            return report
        branch, head = str(found[0]), bytes(found[1])
        schema = await branch_schema(session, domain_id, branch)
        if schema is None:
            report.skipped = "not materialized"
            return report
        stored = {
            str(k): bytes(r)
            for k, r in (
                await session.execute(_STORED, {"domain": domain_id, "branch": branch})
            ).all()
        }
        kinds = sorted(schema.kinds)
        await session.execute(_DROP_OTHERS, {"domain": domain_id, "branch": branch, "kinds": kinds})
        if not force and set(stored) == set(kinds) and all(r == head for r in stored.values()):
            report.skipped = "unchanged"
            return report
    for kind_id in kinds:
        now = clock()
        try:
            async with sessions() as session, session.begin():
                await scope_to_domain(session, domain_id)
                await session.execute(_TIMEOUT, {"timeout": str(settings.statement_timeout_ms)})
                records, sampled, missing, unseen = await _count_kind(
                    session, domain_id, branch, schema.kinds[kind_id], schema, settings, now
                )
                await session.execute(
                    _SAVE,
                    {
                        "domain": domain_id,
                        "branch": branch,
                        "kind": kind_id,
                        "revision": head,
                        "at": now,
                        "records": records,
                        "sampled": sampled,
                        "missing": missing,
                        "unseen": unseen,
                    },
                )
        except Exception as error:
            if not _timed_out(error):
                raise
            report.failed.append(kind_id)
            continue
        report.counted.append(kind_id)
    return report


def _timed_out(error: Exception) -> bool:
    """A statement cancelled by `statement_timeout` (SQLSTATE 57014)."""
    origin: Any = getattr(error, "orig", None)
    for source in (origin, getattr(origin, "__cause__", None)):
        if (getattr(source, "sqlstate", None) or getattr(source, "pgcode", None)) == "57014":
            return True
    return False


async def read_coverage(
    session: AsyncSession, domain_id: str, branch: str, kind_ids: Sequence[str]
) -> dict[str, KindCoverage]:
    """The stored statistics of `kind_ids` on `branch` (none for a Kind not counted), in the
    caller's transaction and row-level security scope."""
    if not kind_ids:
        return {}
    rows = await session.execute(
        _READ, {"domain": domain_id, "branch": branch, "kinds": sorted(set(kind_ids))}
    )
    return {
        str(kind): KindCoverage(
            kind_id=str(kind),
            revision=bytes(revision),
            computed_at=at,
            records=int(records),
            sampled=bool(sampled),
            missing={str(k): int(v) for k, v in dict(missing or {}).items()},
            unseen={str(k): int(v) for k, v in dict(unseen or {}).items()},
        )
        for kind, revision, at, records, sampled, missing, unseen in rows.all()
    }


async def last_runs(
    session: AsyncSession, domain_id: str, source_ids: Sequence[str]
) -> dict[str, datetime | None]:
    """When each Source's last successful run (dry runs aside) finished; None when it never
    succeeded. One index probe per Source."""
    if not source_ids:
        return {}
    rows = await session.execute(
        _LAST_RUNS, {"domain": domain_id, "sources": sorted(set(source_ids))}
    )
    return {str(source): at for source, at in rows.all()}


def stale(last: datetime | None, delta: timedelta | None, now: datetime) -> bool:
    """Whether a Source last synchronized at `last` is past its `stale_after` (`delta`); a
    Source with `stale_after` that never synchronized is stale."""
    if delta is None:
        return False
    return last is None or now - last > delta


__all__ = [
    "MISSING",
    "CoverageReport",
    "KindCoverage",
    "last_runs",
    "read_coverage",
    "run_coverage",
    "stale",
]
