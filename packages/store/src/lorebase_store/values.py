"""The value index (design 8.9): distinct short values of selected Attributes per branch.

`mat_values` holds, per branch, each value of the Attributes listed in `mat_value_attributes`
and how many of the branch's own live rows hold it; triggers on `mat_records` keep it in step
with every write (revision 0045). This module decides the selection, backfills and rebuilds it,
and reads it.

**Selection** (`sync_selection`) is Domain-wide and follows the schema of the default branch:
enums and Attributes marked `resolvable: true` are held; plain strings are held while their
distinct values on the default branch stay within `store.values.max_distinct` and, for Kinds of
at least `ratio_min_records` Records, within `max_distinct_ratio` of the Records; one already
held is dropped only past `hysteresis` times those limits, so an Attribute near them does not
come and go. Sensitive, vault, derived, `resolvable: false` and non-string Attributes never are
(`eligible`). Distinct values are counted when the fingerprint (schema hash and settings)
changed, or every `stats_interval_s`; in between a pass only checks sizes: any branch holding
more than `hard_max_distinct` values of one Attribute (or a plain string past its hysteresis
limit) drops it, so a branch with many distinct values cannot grow the table without bound.

**Fence.** A pass that changes anything takes the Domain's value lock exclusively
(`lb_values_lock_key`, a transaction advisory lock) before it deletes or writes a row; every
statement writing `mat_records` takes it shared in the triggers before reading the selection.
Writers of that Domain wait while the pass holds it (at most `pass_timeout_ms`, a budget for the
whole locked part: each statement gets what is left, Kinds not reached are left to the next
pass), other Domains never do. A pass decides without the lock and takes it only when the
decision changes something (or rows of Attributes no longer held are left), reading the schema
and the selection again under it. Under the lock, an added Attribute's values are deleted and
recounted with one scan of its Kind's rows (all Attributes added for the Kind together, at most
`max_added_per_pass` per pass; the rest are left to the next one), dropped Attributes lose their
rows, and rows of Attributes no longer selected are removed.

**Reads** (`exact_values`, `similar_values`, `indexed_counts`, `live_counts`) take a branch
layout and only return values of selected Attributes. An overlay holds only its own rows'
values: lookups read its base's too (a superset), and readers count the candidates they keep
over the resolved rows (`live_counts`). Everything runs in the caller's transaction, in the
Domain's row-level security scope; trigram matching goes through the security definer function
`lb_values_similar` unless `store.reads.use_definer_functions` is off.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.types import Text

from lorebase_core.infer.phrases import normalize as normalize
from lorebase_core.model import Attribute, Kind, Schema
from lorebase_core.types import AttributeType
from lorebase_store.embeddings import branch_schema, branch_schema_hash
from lorebase_store.jobs import VALUE_INDEX_JOB, enqueue_job, value_index_payload
from lorebase_store.layout import BranchLayout, escape_colons
from lorebase_store.repositories.scope import scope_to_domain
from lorebase_store.runtime import StoreRuntime, default_runtime
from lorebase_store.settings import StoreSettings, ValueIndexSettings
from lorebase_store.telemetry import record_definer_call

MAX_TERMS = 64
"""Terms one call of `lb_values_similar` compares (revision 0045)."""

# Selection --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValueHit:
    """A value matching a term: `similarity` is 1 for an exact match."""

    term: str
    kind_id: str
    attribute_id: str
    value: str
    records: int
    similarity: float = 1.0


Pair = tuple[str, str]
"""(Kind id, Attribute id)."""


@dataclass(slots=True)
class SelectionReport:
    """What a selection pass did. `pending` Attributes are left to the next pass."""

    domain_id: str
    skipped: bool = False
    selected: list[Pair] = field(default_factory=list)
    added: list[Pair] = field(default_factory=list)
    removed: list[Pair] = field(default_factory=list)
    pending: list[Pair] = field(default_factory=list)
    backfilled: int = 0
    orphans: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain_id,
            "skipped": self.skipped,
            "selected": len(self.selected),
            "added": [list(pair) for pair in self.added],
            "removed": [list(pair) for pair in self.removed],
            "pending": [list(pair) for pair in self.pending],
            "backfilled": self.backfilled,
            "orphans": self.orphans,
        }


def _scalar(attribute: Attribute) -> AttributeType | None:
    spec = attribute.type.element if attribute.type.name is AttributeType.ARRAY else attribute.type
    return None if spec is None else spec.name


def eligible(attribute: Attribute) -> bool | None:
    """Whether the value index holds `attribute`: True (always), False (never), None (while
    its distinct values stay within the thresholds)."""
    if attribute.resolvable is False or attribute.sensitivity or attribute.in_vault:
        return False
    if attribute.is_derived or not attribute.value_resolvable:
        return False
    if attribute.resolvable is True or _scalar(attribute) is AttributeType.ENUM:
        return True
    return None


def fingerprint(schema_hash: bytes | None, settings: ValueIndexSettings) -> str:
    """What a selection depends on besides the data: the schema and the settings."""
    digest = hashlib.sha256((schema_hash or b"").hex().encode())
    digest.update(json.dumps(settings.model_dump(mode="json"), sort_keys=True).encode())
    return digest.hexdigest()


_DEFAULT_HEAD = text(
    "SELECT d.default_branch, r.head FROM domains d LEFT JOIN refs r "
    "ON r.domain_id = d.id AND r.name = d.default_branch AND r.deleted_at IS NULL "
    "WHERE d.id = :domain"
)
_CURSOR = text("SELECT fingerprint, updated_at FROM mat_value_cursors WHERE domain_id = :domain")
_SAVE_CURSOR = text(
    "INSERT INTO mat_value_cursors (domain_id, head, fingerprint, updated_at) "
    "VALUES (:domain, :head, :fingerprint, :at) ON CONFLICT (domain_id) DO UPDATE SET "
    "head = EXCLUDED.head, fingerprint = EXCLUDED.fingerprint, updated_at = EXCLUDED.updated_at"
)
_SELECTION = text(
    "SELECT kind_id, attribute_id, max_chars FROM mat_value_attributes WHERE domain_id = :domain"
)
_SIZES = text(
    "SELECT c.kind_id, c.attribute_id, max(c.n) FROM (SELECT v.kind_id, v.attribute_id, "
    "v.branch, count(*) AS n FROM mat_values v WHERE v.domain_id = :domain "
    "GROUP BY v.kind_id, v.attribute_id, v.branch) AS c GROUP BY c.kind_id, c.attribute_id"
)
_STATS = text(
    "SELECT (SELECT count(*) FROM mat_records m WHERE m.domain_id = :domain "
    "AND m.branch = :branch AND m.kind_id = :kind AND NOT m.deleted), "
    "(SELECT count(*) FROM (SELECT DISTINCT v.v FROM mat_records m "
    'CROSS JOIN LATERAL lb_value_strings(m."values" -> :attribute, :max_chars) AS v(v) '
    "WHERE m.domain_id = :domain AND m.branch = :branch AND m.kind_id = :kind "
    "AND NOT m.deleted LIMIT :cap) AS d)"
)
_LOCK_TIMEOUT = text("SELECT set_config('lock_timeout', :lock, true)")
_STATEMENT_TIMEOUT = text("SELECT set_config('statement_timeout', :statement, true)")
_FENCE = text("SELECT pg_advisory_xact_lock(lb_values_lock_key(:domain))")
_SHARED_FENCE = text("SELECT pg_advisory_xact_lock_shared(lb_values_lock_key(:domain))")
_ADD = text(
    "INSERT INTO mat_value_attributes (domain_id, kind_id, attribute_id, max_chars) "
    "SELECT :domain, :kind, a, :max_chars FROM unnest(CAST(:attributes AS text[])) AS a "
    "ON CONFLICT (domain_id, kind_id, attribute_id) DO UPDATE SET max_chars = EXCLUDED.max_chars"
)
_DROP = text(
    "DELETE FROM mat_value_attributes WHERE domain_id = :domain AND kind_id = :kind "
    "AND attribute_id = ANY(CAST(:attributes AS text[]))"
)
_DROP_VALUES = text(
    "DELETE FROM mat_values WHERE domain_id = :domain AND kind_id = :kind "
    "AND attribute_id = ANY(CAST(:attributes AS text[]))"
)
_HAS_ORPHANS = text(
    "SELECT EXISTS (SELECT 1 FROM mat_values v WHERE v.domain_id = :domain AND NOT EXISTS ("
    "SELECT 1 FROM mat_value_attributes s WHERE s.domain_id = v.domain_id "
    "AND s.kind_id = v.kind_id AND s.attribute_id = v.attribute_id))"
)
_ORPHANS = text(
    "DELETE FROM mat_values v WHERE v.domain_id = :domain AND NOT EXISTS ("
    "SELECT 1 FROM mat_value_attributes s WHERE s.domain_id = v.domain_id "
    "AND s.kind_id = v.kind_id AND s.attribute_id = v.attribute_id)"
)
_BACKFILL = text(
    "INSERT INTO mat_values (domain_id, branch, kind_id, attribute_id, value, norm, records) "
    "SELECT m.domain_id, m.branch, m.kind_id, a.attribute_id, v.v, lb_value_norm(v.v), "
    "count(*) FROM mat_records m JOIN refs r ON r.domain_id = m.domain_id AND r.name = m.branch "
    "CROSS JOIN unnest(CAST(:attributes AS text[])) AS a(attribute_id) "
    'CROSS JOIN LATERAL lb_value_strings(m."values" -> a.attribute_id, :max_chars) AS v(v) '
    "WHERE m.domain_id = :domain AND m.kind_id = :kind AND NOT m.deleted "
    "GROUP BY m.domain_id, m.branch, m.kind_id, a.attribute_id, v.v "
    "ON CONFLICT (domain_id, branch, kind_id, attribute_id, value) "
    "DO UPDATE SET records = EXCLUDED.records"
)


async def selection(session: AsyncSession, domain_id: str) -> dict[Pair, int]:
    """The Attributes the index holds, by `(kind id, attribute id)`, with their `max_chars`."""
    rows = await session.execute(_SELECTION, {"domain": domain_id})
    return {(kind, attribute): int(chars) for kind, attribute, chars in rows.all()}


async def _within_thresholds(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    kind: Kind,
    attribute: Attribute,
    settings: ValueIndexSettings,
    factor: float,
) -> bool:
    limit = int(settings.max_distinct * factor)
    records, distinct = (
        await session.execute(
            _STATS,
            {
                "domain": domain_id,
                "branch": branch,
                "kind": kind.id,
                "attribute": attribute.id,
                "max_chars": settings.max_value_chars,
                "cap": limit + 1,
            },
        )
    ).one()
    if int(distinct) > limit:
        return False
    if int(records) >= settings.ratio_min_records:
        return int(distinct) <= settings.max_distinct_ratio * factor * int(records)
    return True


async def wanted_selection(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    schema: Schema,
    settings: ValueIndexSettings,
    held: Mapping[Pair, int] | None = None,
) -> set[Pair]:
    """The Attributes the index should hold, from the schema of the default branch `branch`
    and the distinct values of its rows; those in `held` keep their place up to `hysteresis`
    times the limits."""
    wanted: set[Pair] = set()
    if not settings.enabled:
        return wanted
    held = held or {}
    for kind in schema.kinds.values():
        for attribute in kind.attributes.values():
            decided = eligible(attribute)
            if decided is None:
                factor = settings.hysteresis if (kind.id, attribute.id) in held else 1.0
                decided = await _within_thresholds(
                    session, domain_id, branch, kind, attribute, settings, factor
                )
            if decided:
                wanted.add((kind.id, attribute.id))
    return wanted


def _oversized(
    sizes: Mapping[Pair, int], schema: Schema | None, settings: ValueIndexSettings
) -> set[Pair]:
    """Held Attributes some branch holds too many values of (module doc)."""
    found: set[Pair] = set()
    for (kind_id, attribute_id), size in sizes.items():
        kind = None if schema is None else schema.kinds.get(kind_id)
        attribute = None if kind is None else kind.attributes.get(attribute_id)
        forced = attribute is not None and eligible(attribute) is True
        limit = settings.hard_max_distinct if forced else settings.max_distinct
        if size > limit * (1 if forced else settings.hysteresis):
            found.add((kind_id, attribute_id))
    return found


async def sync_selection(
    session: AsyncSession,
    domain_id: str,
    *,
    settings: StoreSettings,
    force: bool = False,
    now: datetime | None = None,
) -> SelectionReport:
    """Bring the selection of the Domain in line with its default branch (module doc).

    `force` decides again even when the fingerprint and interval say the last decision
    stands. The decision is made without the Domain's value lock; the lock is taken only when
    it changes something (or rows of Attributes no longer held are left), and then the schema
    and the selection are read again under it. Runs in the caller's transaction, in the
    Domain's row-level security scope; commit soon after.
    """
    values = settings.values
    now = now or datetime.now(UTC)
    report = SelectionReport(domain_id)
    found = (await session.execute(_DEFAULT_HEAD, {"domain": domain_id})).one_or_none()
    if found is None:
        raise LookupError(f"Domain {domain_id!r} not found")
    branch, head = found[0], None if found[1] is None else bytes(found[1])
    schema_hash = await branch_schema_hash(session, domain_id, branch)
    marker = fingerprint(schema_hash, values)
    cursor = (await session.execute(_CURSOR, {"domain": domain_id})).one_or_none()
    current = await selection(session, domain_id)
    fresh = (
        not force
        and cursor is not None
        and cursor[0] == marker
        and now - cursor[1] < timedelta(seconds=values.stats_interval_s)
    )
    schema = await branch_schema(session, domain_id, branch)
    wanted = await _decide(session, domain_id, branch, schema, values, current, fresh=fresh)
    if fresh and wanted == set(current):
        report.skipped = True
        report.selected = sorted(current)
        return report
    if not any(_changes(current, wanted, values.max_value_chars)) and not await _orphaned(
        session, domain_id
    ):
        report.selected = sorted(current)
        await _save_cursor(session, domain_id, head, marker, now)
        return report
    # The budget starts with the lock: it bounds how long the Domain's writers wait.
    deadline = time.monotonic() + values.pass_timeout_ms / 1000
    await _fence(session, domain_id, settings, deadline)
    # Read again under the lock: a schema commit or another pass may have moved them.
    moved = await branch_schema_hash(session, domain_id, branch)
    if moved != schema_hash:
        marker = fingerprint(moved, values)
        schema = await branch_schema(session, domain_id, branch)
        current = await selection(session, domain_id)
        wanted = await _decide(session, domain_id, branch, schema, values, current, fresh=False)
    else:
        current = await selection(session, domain_id)
    removed, todo = _changes(current, wanted, values.max_value_chars)
    await _within(session, deadline)
    await _drop(session, domain_id, removed)
    report.removed = removed
    added = todo[: values.max_added_per_pass]
    report.pending = todo[values.max_added_per_pass :]
    report.added, rest, report.backfilled = await _backfill(
        session, domain_id, added, values.max_value_chars, deadline
    )
    report.pending = [*rest, *report.pending]
    await _within(session, deadline)
    report.orphans = max((await session.execute(_ORPHANS, {"domain": domain_id})).rowcount, 0)  # type: ignore[attr-defined]
    report.selected = sorted(wanted - set(report.pending))
    if not report.pending:
        await _save_cursor(session, domain_id, head, marker, now)
    return report


async def _decide(
    session: AsyncSession,
    domain_id: str,
    branch: str,
    schema: Schema | None,
    values: ValueIndexSettings,
    current: Mapping[Pair, int],
    *,
    fresh: bool,
) -> set[Pair]:
    """The wanted selection: counted again unless `fresh` (then the current one), less the
    Attributes some branch holds too many values of."""
    sizes = {
        (kind, attribute): int(n)
        for kind, attribute, n in (await session.execute(_SIZES, {"domain": domain_id})).all()
    }
    oversized = _oversized(sizes, schema, values)
    if fresh:
        return set(current) - oversized
    if schema is None:
        return set()
    wanted = await wanted_selection(session, domain_id, branch, schema, values, current)
    return wanted - oversized


def _changes(
    current: Mapping[Pair, int], wanted: set[Pair], chars: int
) -> tuple[list[Pair], list[Pair]]:
    """(pairs to drop, pairs to count: those whose max_chars changed, then the new ones)."""
    removed = sorted(pair for pair in current if pair not in wanted)
    changed = sorted(pair for pair, held in current.items() if pair in wanted and held != chars)
    return removed, [*changed, *sorted(wanted - set(current))]


async def _orphaned(session: AsyncSession, domain_id: str) -> bool:
    return bool(await session.scalar(_HAS_ORPHANS, {"domain": domain_id}))


async def _save_cursor(
    session: AsyncSession, domain_id: str, head: bytes | None, marker: str, now: datetime
) -> None:
    await session.execute(
        _SAVE_CURSOR, {"domain": domain_id, "head": head or b"", "fingerprint": marker, "at": now}
    )


def _by_kind(pairs: Sequence[Pair]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for kind, attribute in pairs:
        grouped[kind].append(attribute)
    return grouped


async def _fence(
    session: AsyncSession, domain_id: str, settings: StoreSettings, deadline: float
) -> None:
    """The Domain's value lock, exclusive, waiting at most `store.commit.lock_timeout_ms`
    (module doc)."""
    await session.execute(_LOCK_TIMEOUT, {"lock": f"{settings.commit.lock_timeout_ms}ms"})
    await _within(session, deadline)
    await session.execute(_FENCE, {"domain": domain_id})


class PassBudgetExceededError(Exception):
    """The pass used `store.values.pass_timeout_ms` before a step it had to take."""


async def _within(session: AsyncSession, deadline: float) -> float:
    """The time left before `deadline`, in seconds, set as the statement timeout of what
    follows: writers of the Domain wait at most the whole pass budget, not a budget per
    statement. Raises when nothing is left."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise PassBudgetExceededError("the value index pass used its time budget")
    await session.execute(_STATEMENT_TIMEOUT, {"statement": f"{max(int(left * 1000), 1)}ms"})
    return left


async def _drop(session: AsyncSession, domain_id: str, pairs: Sequence[Pair]) -> None:
    for kind, attributes in sorted(_by_kind(pairs).items()):
        params = {"domain": domain_id, "kind": kind, "attributes": attributes}
        await session.execute(_DROP, params)
        await session.execute(_DROP_VALUES, params)


async def _backfill(
    session: AsyncSession,
    domain_id: str,
    pairs: Sequence[Pair],
    chars: int,
    deadline: float | None = None,
) -> tuple[list[Pair], list[Pair], int]:
    """Select `pairs` and count their values on every branch: one scan per Kind. The caller
    holds the fence. With `deadline`, Kinds are counted while budget is left; returns (pairs
    done, pairs left, rows written). A scan that outlives the budget fails the pass (rolled
    back, retried)."""
    written = 0
    done: list[Pair] = []
    grouped = sorted(_by_kind(pairs).items())
    for index, (kind, attributes) in enumerate(grouped):
        if deadline is not None and deadline - time.monotonic() <= 0:
            left = [(k, a) for k, names in grouped[index:] for a in sorted(names)]
            return done, left, written
        if deadline is not None:
            await _within(session, deadline)
        params: dict[str, Any] = {
            "domain": domain_id,
            "kind": kind,
            "attributes": sorted(attributes),
            "max_chars": chars,
        }
        await session.execute(_DROP_VALUES, params)
        await session.execute(_ADD, params)
        result = await session.execute(_BACKFILL, params)
        written += max(result.rowcount or 0, 0)  # type: ignore[attr-defined]
        done += [(kind, a) for a in sorted(attributes)]
    return done, [], written


async def drop_ineligible(
    session: AsyncSession, domain_id: str, branch: str, schema: Schema
) -> list[Pair]:
    """In the transaction of a commit or merge that changed the schema of `branch`: when it
    is the default branch, drop at once the Attributes `schema` no longer lets the index hold
    (sensitive, vault, retyped, derived, `resolvable: false`, or gone) with their values, and
    queue a `value_index` pass for the rest (additions, thresholds, stragglers a concurrent
    writer counted). Takes the Domain's value lock shared first, like the triggers, so it waits
    for a running pass and a later pass sees this schema; the deletes use the selection's key
    and `ix_mat_values_attribute`."""
    found = (await session.execute(_DEFAULT_HEAD, {"domain": domain_id})).one_or_none()
    if found is None or found[0] != branch:
        return []
    await session.execute(_SHARED_FENCE, {"domain": domain_id})
    held = await selection(session, domain_id)
    gone: list[Pair] = []
    for kind_id, attribute_id in sorted(held):
        kind = schema.kinds.get(kind_id)
        attribute = None if kind is None else kind.attributes.get(attribute_id)
        if attribute is None or eligible(attribute) is False:
            gone.append((kind_id, attribute_id))
    await _drop(session, domain_id, gone)
    # Not deduplicated against a running pass: it decided on the schema before this one.
    await enqueue_job(
        session,
        VALUE_INDEX_JOB,
        domain_id=domain_id,
        payload=value_index_payload(),
        unique_queued=True,
    )
    return gone


async def rebuild(
    session: AsyncSession, domain_id: str, *, settings: StoreSettings, now: datetime | None = None
) -> SelectionReport:
    """Decide the selection again and recount every value of the Domain from its rows: the
    repair path for an index that drifted (or predates its selection). The recount has a
    `pass_timeout_ms` budget of its own after the selection pass."""
    report = await sync_selection(session, domain_id, settings=settings, force=True, now=now)
    deadline = time.monotonic() + settings.values.pass_timeout_ms / 1000
    await _fence(session, domain_id, settings, deadline)
    held = await selection(session, domain_id)
    for kind, attributes in sorted(_by_kind(sorted(held)).items()):
        await session.execute(
            _DROP_VALUES, {"domain": domain_id, "kind": kind, "attributes": attributes}
        )
    report.backfilled = 0
    by_chars: dict[int, list[Pair]] = defaultdict(list)
    for pair, chars in held.items():
        by_chars[chars].append(pair)
    for chars, pairs in sorted(by_chars.items()):
        report.backfilled += (await _backfill(session, domain_id, sorted(pairs), chars))[2]
    return report


async def run_value_index(
    sessions: async_sessionmaker[AsyncSession],
    domain_id: str,
    *,
    settings: StoreSettings,
    rebuild_all: bool = False,
) -> SelectionReport:
    """One pass over a Domain in its own transaction, in the Domain's row-level security
    scope: `sync_selection`, or `rebuild` with `rebuild_all`. A pass that left Attributes
    pending queues the next one."""
    async with sessions() as session, session.begin():
        await scope_to_domain(session, domain_id)
        if rebuild_all:
            report = await rebuild(session, domain_id, settings=settings)
        else:
            report = await sync_selection(session, domain_id, settings=settings)
        if report.pending:
            await enqueue_job(
                session,
                VALUE_INDEX_JOB,
                domain_id=domain_id,
                payload=value_index_payload(),
                unique=True,
            )
        return report


# Reads ------------------------------------------------------------------------------------


def _branches(layout: BranchLayout) -> list[str]:
    return [layout.branch, *([layout.base] if layout.base else [])]


_SELECTED = (
    "JOIN mat_value_attributes s ON s.domain_id = v.domain_id AND s.kind_id = v.kind_id "
    "AND s.attribute_id = v.attribute_id "
)
"""Reads see values of selected Attributes only, whatever rows a race left behind."""
_PAIRS = (
    "(CAST(:pair_kinds AS text[]) IS NULL OR (v.kind_id, v.attribute_id) IN ("
    "SELECT * FROM unnest(CAST(:pair_kinds AS text[]), CAST(:pair_attributes AS text[])))) "
)
"""Only the (Kind, Attribute) pairs the caller may read values of, before any limit."""
_EXACT = text(
    "WITH t AS MATERIALIZED (SELECT DISTINCT x.term, lb_value_norm(x.term) AS norm "
    "FROM unnest(CAST(:terms AS text[])) AS x(term)) "
    "SELECT h.term, h.kind_id, h.attribute_id, h.value, h.records FROM ("
    "SELECT t.term, v.kind_id, v.attribute_id, v.value, CAST(sum(v.records) AS bigint) AS records, "
    "row_number() OVER (PARTITION BY t.term ORDER BY sum(v.records) DESC, v.value) AS n "
    "FROM t JOIN mat_values v ON v.domain_id = :domain "
    "AND v.branch = ANY(CAST(:branches AS text[])) AND v.norm = t.norm AND v.records > 0 "
    + _SELECTED
    + "WHERE "
    + _PAIRS
    + "GROUP BY t.term, v.kind_id, v.attribute_id, v.value) AS h WHERE h.n <= :per_term "
    "ORDER BY h.term, h.records DESC, h.value"
)
_SIMILAR_DEFINER = text(
    "SELECT term, kind_id, attribute_id, value, records, similarity FROM lb_values_similar("
    ":domain, :branch, CAST(:pair_kinds AS text[]), CAST(:pair_attributes AS text[]), "
    "CAST(:terms AS text[]), "
    "CAST(:threshold AS real), CAST(:limit AS integer))"
)
_SIMILARITY_THRESHOLD = text(
    "SELECT set_config('pg_trgm.similarity_threshold', "
    "CAST(CAST(:threshold AS real) AS text), true)"
)
_SIMILAR_SQL = text(
    "SELECT CAST(:term AS text), v.kind_id, v.attribute_id, v.value, "
    "CAST(sum(v.records) AS bigint), CAST(max(similarity(v.norm, lb_value_norm(:term))) AS real) "
    "FROM mat_values v "
    + _SELECTED
    + "WHERE v.domain_id = :domain AND v.branch = ANY(CAST(:branches AS text[])) "
    "AND v.records > 0 AND v.norm % lb_value_norm(:term) AND "
    + _PAIRS
    + "GROUP BY v.kind_id, v.attribute_id, v.value ORDER BY 6 DESC, 5 DESC, v.value LIMIT :limit"
)


def _pair_params(pairs: Sequence[Pair] | None) -> dict[str, list[str] | None]:
    if pairs is None:
        return {"pair_kinds": None, "pair_attributes": None}
    return {"pair_kinds": [k for k, _ in pairs], "pair_attributes": [a for _, a in pairs]}


def _hits(rows: Sequence[Any]) -> list[ValueHit]:
    return [
        ValueHit(str(r[0]), str(r[1]), str(r[2]), str(r[3]), int(r[4]), float(r[5]))
        if len(r) > 5
        else ValueHit(str(r[0]), str(r[1]), str(r[2]), str(r[3]), int(r[4]))
        for r in rows
    ]


async def exact_values(
    session: AsyncSession,
    layout: BranchLayout,
    terms: Sequence[str],
    *,
    pairs: Sequence[Pair] | None = None,
    per_term: int,
) -> list[ValueHit]:
    """Values whose compared form equals that of a term, of the `(kind, attribute)` `pairs`
    (`None`: every one), at most `per_term` per term (most held first)."""
    if not terms:
        return []
    params = {
        "domain": layout.domain_id,
        "branches": _branches(layout),
        "terms": list(terms),
        **_pair_params(pairs),
        "per_term": per_term,
    }
    return _hits((await session.execute(_EXACT, params)).all())


async def similar_values(
    session: AsyncSession,
    layout: BranchLayout,
    terms: Sequence[str],
    *,
    threshold: float,
    per_term: int,
    pairs: Sequence[Pair] | None = None,
    runtime: StoreRuntime | None = None,
) -> list[ValueHit]:
    """For each term, its `per_term` most similar values of the `(kind, attribute)` `pairs`
    (`None`: every one; trigram similarity of the compared forms, at least `threshold`), best
    first."""
    settings = (runtime or default_runtime()).settings
    restriction = _pair_params(pairs)
    found: list[ValueHit] = []
    if settings.reads.use_definer_functions:
        per_term = min(per_term, settings.reads.definer_ceilings.limit)
        for start in range(0, len(terms), MAX_TERMS):
            record_definer_call("lb_values_similar")
            params = {
                "domain": layout.domain_id,
                "branch": layout.branch,
                **restriction,
                "terms": list(terms[start : start + MAX_TERMS]),
                "threshold": threshold,
                "limit": per_term,
            }
            found.extend(_hits((await session.execute(_SIMILAR_DEFINER, params)).all()))
        return found
    await session.execute(_SIMILARITY_THRESHOLD, {"threshold": threshold})
    for term in terms:
        if not normalize(term):
            continue
        params = {
            "domain": layout.domain_id,
            "branches": _branches(layout),
            **restriction,
            "term": term,
            "limit": per_term,
        }
        found.extend(_hits((await session.execute(_SIMILAR_SQL, params)).all()))
    return found


_INDEXED_COUNTS = text(
    "SELECT v.value, v.records FROM mat_values v "
    + _SELECTED
    + "WHERE v.domain_id = :domain AND v.branch = :branch AND v.kind_id = :kind "
    "AND v.attribute_id = :attribute AND v.value = ANY(CAST(:wanted AS text[]))"
)


async def indexed_counts(
    session: AsyncSession,
    layout: BranchLayout,
    kind_id: str,
    attribute_id: str,
    values: Sequence[str],
) -> dict[str, int]:
    """How many rows of a full branch hold each of `values` in a held Attribute, from the
    index (0 for a value it does not hold); an overlay needs `live_counts`."""
    if layout.overlay:
        raise ValueError("an overlay's index holds its own rows only: use live_counts")
    rows = await session.execute(
        _INDEXED_COUNTS,
        {
            "domain": layout.domain_id,
            "branch": layout.branch,
            "kind": kind_id,
            "attribute": attribute_id,
            "wanted": list(values),
        },
    )
    counts = {str(v): int(n) for v, n in rows.all()}
    return {v: counts.get(v, 0) for v in values}


async def live_counts(
    session: AsyncSession,
    layout: BranchLayout,
    kind_id: str,
    attribute_id: str,
    values: Sequence[str],
    *,
    array: bool = False,
) -> dict[str, int]:
    """How many of the resolved rows of the branch hold each of `values` in the Attribute
    (an element of it with `array`): the count an overlay's readers report. Each value is a
    containment probe of the GIN index on `values`."""
    if not values:
        return {}
    probes = [{attribute_id: [v] if array else v} for v in values]
    names = [f"p{i}" for i in range(len(probes))]
    contained = " OR ".join(f'm."values" @> :{n}' for n in names)
    statement = text(
        f"SELECT v.v, count(*) FROM {escape_colons(layout.records_from('m'))} "
        'CROSS JOIN LATERAL lb_value_strings(m."values" -> :attribute, :max_chars) AS v(v) '
        "WHERE m.domain_id = :domain AND m.branch = :branch AND m.kind_id = :kind "
        f"AND NOT m.deleted AND ({contained}) AND v.v = ANY(:wanted) GROUP BY v.v"
    ).bindparams(
        *(bindparam(n, type_=JSONB) for n in names),
        bindparam("wanted", type_=ARRAY(Text())),
    )
    params: dict[str, Any] = {
        "domain": layout.domain_id,
        "branch": layout.branch,
        "kind": kind_id,
        "attribute": attribute_id,
        "max_chars": max(len(v) for v in values),
        "wanted": list(values),
        **dict(zip(names, probes, strict=True)),
    }
    rows = await session.execute(statement, params)
    counts: Mapping[str, int] = {str(v): int(n) for v, n in rows.all()}
    return {v: counts.get(v, 0) for v in values}
