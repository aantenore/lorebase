"""Coverage statistics of the default branch head (design 8.11): what reads report about how
complete an answer can be.

`mat_coverage` holds, per Kind of the default branch head, the live Records and, per stored
Attribute, how many of them miss a value: the Attribute absent, JSON null, an empty string or
an empty list (`MISSING`). For each incremental Source with `stale_after` that writes the
Kind, it also holds how many of the Records it owns no run has seen for that long
(`source_written`, design 9.1).

`run_coverage` counts a Kind when the head moved since its statistics were counted (or with
`force`), unless they are younger than `store.coverage.min_interval_s`; each Kind in one
REPEATABLE READ transaction under `store.coverage.statement_timeout_ms`, so its Records,
missing values and the Revision they are labelled with come from one snapshot, read in one
pass over the Kind's rows: the first `max_records_per_kind` in key order, a larger Kind's
missing counts scaled to its Records (`sampled`). Unseen Records depend on the time as well,
so every pass recounts them (`unseen_at`), the head moved or not. Reads (`read_coverage`,
`last_runs`) are primary key lookups and one probe of `ix_source_runs_last_succeeded` per
Source, so a read pays two small statements however many Records and runs there are.

The statistics are counted over every row of the Kind, values of sensitive Attributes
included: readers decide what a caller may see (Kinds in its scope, Attributes it may read
values of). They describe the head they were counted at (`revision`); readers mark them
approximate once the head moved, or once `unseen` is old. Branches other than the default
one, and older Revisions, have none.
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
    "SELECT kind_id, revision, computed_at FROM mat_coverage "
    "WHERE domain_id = :domain AND branch = :branch"
)
_READ = text(
    "SELECT kind_id, revision, computed_at, records, sampled, missing, unseen, unseen_at "
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
    "sampled, missing, unseen, unseen_at) VALUES (:domain, :branch, :kind, :revision, :at, "
    ":records, :sampled, :missing, :unseen, :at) ON CONFLICT (domain_id, branch, kind_id) "
    "DO UPDATE SET revision = EXCLUDED.revision, computed_at = EXCLUDED.computed_at, "
    "records = EXCLUDED.records, sampled = EXCLUDED.sampled, missing = EXCLUDED.missing, "
    "unseen = EXCLUDED.unseen, unseen_at = EXCLUDED.unseen_at"
).bindparams(bindparam("missing", type_=JSONB), bindparam("unseen", type_=JSONB))
_SAVE_UNSEEN = text(
    "UPDATE mat_coverage SET unseen = :unseen, unseen_at = :at WHERE domain_id = :domain "
    "AND branch = :branch AND kind_id = :kind"
).bindparams(bindparam("unseen", type_=JSONB))
_REPEATABLE = text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
_DROP_OTHERS = text(
    "DELETE FROM mat_coverage WHERE domain_id = :domain "
    "AND (branch <> :branch OR NOT (kind_id = ANY(CAST(:kinds AS text[]))))"
)
_LAST_RUNS = text(
    "SELECT s.id, (SELECT r.finished_at FROM source_runs r WHERE r.domain_id = :domain "
    "AND r.source_id = s.id AND r.status = 'succeeded' "
    "AND coalesce(r.stats ->> 'dry_run', 'false') <> 'true' AND r.started_at IS NOT NULL "
    "ORDER BY r.started_at DESC LIMIT 1) "
    "FROM unnest(CAST(:sources AS text[])) AS s(id)"
)
"""Its predicate is the one of `ix_source_runs_last_succeeded` (revision 0046): one probe."""
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
    unseen_at: datetime | None = None


@dataclass(slots=True)
class CoverageReport:
    """What a pass did: Kinds counted, Kinds whose count failed (kept as they were)."""

    domain_id: str
    skipped: str | None = None
    counted: list[str] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    """Kinds whose unseen Records only were counted again."""
    failed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain_id,
            "skipped": self.skipped,
            "counted": len(self.counted),
            "refreshed": len(self.refreshed),
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
        "AND m.kind_id = :kind AND NOT m.deleted ORDER BY m.key LIMIT :cap) AS m"
    )


async def _unseen(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    kind: Kind,
    schema: Schema,
    now: datetime,
) -> dict[str, int]:
    """Per incremental Source with `stale_after` writing `kind`: its Records unseen that long."""
    unseen: dict[str, int] = {}
    for source in schema.sources.values():
        delta = source.stale_after_delta
        if source.target_kind != kind.id or source.sync_mode is not SyncMode.INCREMENTAL:
            continue
        if delta is None:
            continue
        found = await session.execute(
            _UNSEEN,
            {
                "domain": domain_id,
                "branch": branch,
                "kind": kind.id,
                "source": source.id,
                "before": now - delta,
            },
        )
        unseen[source.id] = int(found.scalar_one())
    return unseen


async def _count_kind(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    kind: Kind,
    settings: CoverageSettings,
) -> tuple[int, bool, dict[str, int]]:
    """Records, whether sampled, and missing values per Attribute, from one snapshot (the
    caller's REPEATABLE READ transaction): one pass over the first `max_records_per_kind`
    rows in key order, and a count of the Kind's rows only when there are more."""
    attributes = _attributes(kind)
    params = {"domain": domain_id, "branch": branch, "kind": kind.id}
    cap = settings.max_records_per_kind
    row = (
        await session.execute(
            _stats_statement(attributes),
            {**params, "cap": cap, **{f"a{i}": a for i, a in enumerate(attributes)}},
        )
    ).one()
    counted = int(row[0])
    records = counted
    if counted >= cap:
        records = int((await session.execute(_RECORDS, params)).scalar_one())
    sampled = records > counted
    scale = records / counted if sampled and counted else 1.0
    missing = {a: min(records, round(int(row[i + 1]) * scale)) for i, a in enumerate(attributes)}
    return records, sampled, missing


async def run_coverage(
    sessions: async_sessionmaker[AsyncSession],
    domain_id: str,
    *,
    settings: CoverageSettings,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    force: bool = False,
) -> CoverageReport:
    """Count the statistics of the Domain's default branch head (module doc); each Kind in
    its own transaction, so a Kind whose count times out keeps its previous statistics and
    the others are still counted."""
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
            str(k): (bytes(r), at)
            for k, r, at in (
                await session.execute(_STORED, {"domain": domain_id, "branch": branch})
            ).all()
        }
        kinds = sorted(schema.kinds)
        await session.execute(_DROP_OTHERS, {"domain": domain_id, "branch": branch, "kinds": kinds})
    recent = clock() - timedelta(seconds=settings.min_interval_s)
    for kind_id in kinds:
        previous = stored.get(kind_id)
        count = force or previous is None or (previous[0] != head and previous[1] <= recent)
        kind = schema.kinds[kind_id]
        try:
            async with sessions() as session, session.begin():
                await session.execute(_REPEATABLE)
                await scope_to_domain(session, domain_id)
                await session.execute(_TIMEOUT, {"timeout": str(settings.statement_timeout_ms)})
                at = clock()
                unseen = await _unseen(session, domain_id, branch, kind, schema, at)
                base = {"domain": domain_id, "branch": branch, "kind": kind_id, "at": at}
                if not count:
                    await session.execute(_SAVE_UNSEEN, {**base, "unseen": unseen})
                    report.refreshed.append(kind_id)
                    continue
                found = (await session.execute(_DEFAULT_HEAD, {"domain": domain_id})).one()
                records, sampled, missing = await _count_kind(
                    session, domain_id, branch, kind, settings
                )
                await session.execute(
                    _SAVE,
                    {
                        **base,
                        "revision": bytes(found[1]),
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
    if not report.counted and not report.failed:
        report.skipped = "unchanged"
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
            unseen_at=unseen_at,
        )
        for kind, revision, at, records, sampled, missing, unseen, unseen_at in rows.all()
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
