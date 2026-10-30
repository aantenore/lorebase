"""The value index (design 8.9): distinct short values of selected Attributes per branch.

`mat_values` holds, per branch, each value of the Attributes listed in `mat_value_attributes`
and how many of the branch's own live rows hold it; triggers on `mat_records` keep it in step
with every write (revision 0045). This module decides the selection, backfills and rebuilds it,
and reads it.

**Selection** (`sync_selection`) is Domain-wide and follows the schema of the default branch:
enums and Attributes marked `resolvable: true` are held; plain strings are held while their
distinct values on the default branch stay within `store.values.max_distinct` and, for Kinds of
at least `ratio_min_records` Records, within `max_distinct_ratio` of the Records. Sensitive
Attributes, `resolvable: false` ones and other types never are (`Attribute.value_resolvable`).
A pass whose default branch head and fingerprint (schema hash and settings) did not move since
the last one (`mat_value_cursors`) does nothing.

Adding Attributes backfills their values on every branch of the Domain under a `SHARE` lock on
`mat_records` (writers wait for it, readers do not), so no write lands between the backfill and
the selection becoming visible to the triggers; removing one deletes its rows.

**Reads** (`exact_values`, `similar_values`, `live_counts`) take a branch layout. An overlay
holds only its own rows' values: lookups read its base's too (a superset), and readers count the
candidates they keep over the resolved rows (`live_counts`). Everything runs in the caller's
transaction, in the Domain's row-level security scope; trigram matching goes through the
security definer function `lb_values_similar` unless `store.reads.use_definer_functions` is off.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.types import Text

from lorebase_core.model import Attribute, Kind, Schema
from lorebase_core.types import AttributeType
from lorebase_store.embeddings import branch_schema, branch_schema_hash
from lorebase_store.layout import BranchLayout, escape_colons
from lorebase_store.repositories.scope import scope_to_domain
from lorebase_store.runtime import StoreRuntime, default_runtime
from lorebase_store.settings import StoreSettings, ValueIndexSettings
from lorebase_store.telemetry import record_definer_call

MAX_TERMS = 64
"""Terms one call of `lb_values_similar` compares (revision 0045)."""

_COMBINING = re.compile("[̀-ͯ]")
_BLANKS = re.compile(r"\s+")


def normalize(value: str) -> str:
    """The compared form of a value or a word, as `lb_value_norm` computes it: lower case,
    accents stripped, blanks collapsed."""
    decomposed = unicodedata.normalize("NFD", value.lower())
    return _BLANKS.sub(" ", _COMBINING.sub("", decomposed)).strip()


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


@dataclass(slots=True)
class SelectionReport:
    """What a selection pass did."""

    domain_id: str
    skipped: bool = False
    selected: list[tuple[str, str]] = field(default_factory=list)
    added: list[tuple[str, str]] = field(default_factory=list)
    removed: list[tuple[str, str]] = field(default_factory=list)
    backfilled: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain_id,
            "skipped": self.skipped,
            "selected": len(self.selected),
            "added": [list(pair) for pair in self.added],
            "removed": [list(pair) for pair in self.removed],
            "backfilled": self.backfilled,
        }


def _scalar(attribute: Attribute) -> AttributeType | None:
    spec = attribute.type.element if attribute.type.name is AttributeType.ARRAY else attribute.type
    return None if spec is None else spec.name


def eligible(attribute: Attribute) -> bool | None:
    """Whether the value index holds `attribute`: True (always), False (never), None (while
    its distinct values stay within the thresholds)."""
    if attribute.resolvable is False or attribute.sensitivity or attribute.in_vault:
        return False
    if not attribute.value_resolvable:
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
_CURSOR = text("SELECT head, fingerprint FROM mat_value_cursors WHERE domain_id = :domain")
_SAVE_CURSOR = text(
    "INSERT INTO mat_value_cursors (domain_id, head, fingerprint, updated_at) "
    "VALUES (:domain, :head, :fingerprint, :at) ON CONFLICT (domain_id) DO UPDATE SET "
    "head = EXCLUDED.head, fingerprint = EXCLUDED.fingerprint, updated_at = EXCLUDED.updated_at"
)
_SELECTION = text(
    "SELECT kind_id, attribute_id, max_chars FROM mat_value_attributes WHERE domain_id = :domain"
)
_STATS = text(
    "SELECT (SELECT count(*) FROM mat_records m WHERE m.domain_id = :domain "
    "AND m.branch = :branch AND m.kind_id = :kind AND NOT m.deleted), "
    "(SELECT count(*) FROM (SELECT DISTINCT v.v FROM mat_records m "
    'CROSS JOIN LATERAL lb_value_strings(m."values" -> :attribute, :max_chars) AS v(v) '
    "WHERE m.domain_id = :domain AND m.branch = :branch AND m.kind_id = :kind "
    "AND NOT m.deleted LIMIT :cap) AS d)"
)
_LOCK = text("LOCK TABLE mat_records IN SHARE MODE")
_LOCK_TIMEOUT = text("SELECT set_config('lock_timeout', :timeout, true)")
_ADD = text(
    "INSERT INTO mat_value_attributes (domain_id, kind_id, attribute_id, max_chars) "
    "VALUES (:domain, :kind, :attribute, :max_chars)"
)
_DROP = text(
    "DELETE FROM mat_value_attributes WHERE domain_id = :domain AND kind_id = :kind "
    "AND attribute_id = :attribute"
)
_DROP_VALUES = text(
    "DELETE FROM mat_values WHERE domain_id = :domain AND kind_id = :kind "
    "AND attribute_id = :attribute"
)
_DROP_ALL_VALUES = text("DELETE FROM mat_values WHERE domain_id = :domain")
_BACKFILL = text(
    "INSERT INTO mat_values (domain_id, branch, kind_id, attribute_id, value, norm, records) "
    "SELECT m.domain_id, m.branch, m.kind_id, CAST(:attribute AS text), v.v, lb_value_norm(v.v), "
    "count(*) FROM mat_records m JOIN refs r ON r.domain_id = m.domain_id AND r.name = m.branch "
    'CROSS JOIN LATERAL lb_value_strings(m."values" -> :attribute, :max_chars) AS v(v) '
    "WHERE m.domain_id = :domain AND m.kind_id = :kind AND NOT m.deleted "
    "GROUP BY m.domain_id, m.branch, m.kind_id, v.v"
)


async def selection(session: AsyncSession, domain_id: str) -> dict[tuple[str, str], int]:
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
) -> bool:
    records, distinct = (
        await session.execute(
            _STATS,
            {
                "domain": domain_id,
                "branch": branch,
                "kind": kind.id,
                "attribute": attribute.id,
                "max_chars": settings.max_value_chars,
                "cap": settings.max_distinct + 1,
            },
        )
    ).one()
    if int(distinct) > settings.max_distinct:
        return False
    if int(records) >= settings.ratio_min_records:
        return int(distinct) <= settings.max_distinct_ratio * int(records)
    return True


async def wanted_selection(
    session: AsyncSession, domain_id: str, branch: str, schema: Schema, settings: ValueIndexSettings
) -> set[tuple[str, str]]:
    """The Attributes the index should hold, from the schema of the default branch `branch`
    and the distinct values of its rows."""
    wanted: set[tuple[str, str]] = set()
    if not settings.enabled:
        return wanted
    for kind in schema.kinds.values():
        for attribute in kind.attributes.values():
            if attribute.is_derived:
                continue
            decided = eligible(attribute)
            if decided is None:
                decided = await _within_thresholds(
                    session, domain_id, branch, kind, attribute, settings
                )
            if decided:
                wanted.add((kind.id, attribute.id))
    return wanted


async def sync_selection(
    session: AsyncSession,
    domain_id: str,
    *,
    settings: StoreSettings,
    force: bool = False,
    now: datetime | None = None,
) -> SelectionReport:
    """Bring the selection of the Domain in line with its default branch (module doc).

    `force` runs even when the cursor says nothing moved. Runs in the caller's transaction,
    in the Domain's row-level security scope.
    """
    report = SelectionReport(domain_id)
    found = (await session.execute(_DEFAULT_HEAD, {"domain": domain_id})).one_or_none()
    if found is None:
        raise LookupError(f"Domain {domain_id!r} not found")
    branch, head = found[0], None if found[1] is None else bytes(found[1])
    schema_hash = await branch_schema_hash(session, domain_id, branch)
    marker = fingerprint(schema_hash, settings.values)
    cursor = (await session.execute(_CURSOR, {"domain": domain_id})).one_or_none()
    current = await selection(session, domain_id)
    if (
        not force
        and cursor is not None
        and head is not None
        and bytes(cursor[0]) == head
        and cursor[1] == marker
    ):
        report.skipped = True
        report.selected = sorted(current)
        return report
    schema = await branch_schema(session, domain_id, branch)
    wanted = (
        set()
        if schema is None
        else await wanted_selection(session, domain_id, branch, schema, settings.values)
    )
    chars = settings.values.max_value_chars
    # A changed max_chars re-reads every value: drop and add again.
    stale = {pair for pair, held in current.items() if pair not in wanted or held != chars}
    added = sorted(wanted - (set(current) - stale))
    await _apply(session, domain_id, sorted(stale), added, chars, settings, report)
    report.selected = sorted(wanted)
    if head is not None:
        await session.execute(
            _SAVE_CURSOR,
            {
                "domain": domain_id,
                "head": head,
                "fingerprint": marker,
                "at": now or datetime.now(UTC),
            },
        )
    return report


async def _apply(
    session: AsyncSession,
    domain_id: str,
    removed: Sequence[tuple[str, str]],
    added: Sequence[tuple[str, str]],
    chars: int,
    settings: StoreSettings,
    report: SelectionReport,
) -> None:
    for kind, attribute in removed:
        dropped = {"domain": domain_id, "kind": kind, "attribute": attribute}
        await session.execute(_DROP, dropped)
        await session.execute(_DROP_VALUES, dropped)
    report.removed = [pair for pair in removed if pair not in added]
    if not added:
        return
    await _lock(session, settings)
    for kind, attribute in added:
        params: dict[str, Any] = {
            "domain": domain_id,
            "kind": kind,
            "attribute": attribute,
            "max_chars": chars,
        }
        await session.execute(_ADD, params)
        result = await session.execute(_BACKFILL, params)
        report.backfilled += max(result.rowcount or 0, 0)  # type: ignore[attr-defined]
    report.added = [pair for pair in added if pair not in removed]


async def _lock(session: AsyncSession, settings: StoreSettings) -> None:
    """Writers wait while a backfill reads `mat_records` (module doc)."""
    timeout = f"{settings.commit.lock_timeout_ms}ms"
    await session.execute(_LOCK_TIMEOUT, {"timeout": timeout})
    await session.execute(_LOCK)


async def rebuild(
    session: AsyncSession, domain_id: str, *, settings: StoreSettings, now: datetime | None = None
) -> SelectionReport:
    """Decide the selection again and recount every value of the Domain from its rows: the
    repair path for an index that drifted (or predates its selection)."""
    report = await sync_selection(session, domain_id, settings=settings, force=True, now=now)
    held = await selection(session, domain_id)
    await _lock(session, settings)
    await session.execute(_DROP_ALL_VALUES, {"domain": domain_id})
    report.backfilled = 0
    for (kind, attribute), chars in sorted(held.items()):
        params = {"domain": domain_id, "kind": kind, "attribute": attribute, "max_chars": chars}
        result = await session.execute(_BACKFILL, params)
        report.backfilled += max(result.rowcount or 0, 0)  # type: ignore[attr-defined]
    return report


async def run_value_index(
    sessions: async_sessionmaker[AsyncSession],
    domain_id: str,
    *,
    settings: StoreSettings,
    rebuild_all: bool = False,
) -> SelectionReport:
    """One pass over a Domain in its own transaction, in the Domain's row-level security
    scope: `sync_selection`, or `rebuild` with `rebuild_all`."""
    async with sessions() as session, session.begin():
        await scope_to_domain(session, domain_id)
        if rebuild_all:
            return await rebuild(session, domain_id, settings=settings)
        return await sync_selection(session, domain_id, settings=settings)


# Reads ------------------------------------------------------------------------------------


def _branches(layout: BranchLayout) -> list[str]:
    return [layout.branch, *([layout.base] if layout.base else [])]


_EXACT = text(
    "SELECT t.term, v.kind_id, v.attribute_id, v.value, CAST(sum(v.records) AS bigint) "
    "FROM unnest(CAST(:terms AS text[])) AS t(term) JOIN mat_values v "
    "ON v.domain_id = :domain AND v.branch = ANY(CAST(:branches AS text[])) "
    "AND v.norm = lb_value_norm(t.term) AND v.records > 0 "
    "AND (CAST(:kinds AS text[]) IS NULL OR v.kind_id = ANY(CAST(:kinds AS text[]))) "
    "GROUP BY t.term, v.kind_id, v.attribute_id, v.value "
    "ORDER BY t.term, 5 DESC, v.value LIMIT :limit"
)
_SIMILAR_DEFINER = text(
    "SELECT term, kind_id, attribute_id, value, records, similarity FROM lb_values_similar("
    ":domain, :branch, CAST(:kinds AS text[]), CAST(:terms AS text[]), "
    "CAST(:threshold AS real), CAST(:limit AS integer))"
)
_SIMILARITY_THRESHOLD = text(
    "SELECT set_config('pg_trgm.similarity_threshold', "
    "CAST(CAST(:threshold AS real) AS text), true)"
)
_SIMILAR_SQL = text(
    "SELECT CAST(:term AS text), v.kind_id, v.attribute_id, v.value, "
    "CAST(sum(v.records) AS bigint), CAST(max(similarity(v.norm, lb_value_norm(:term))) AS real) "
    "FROM mat_values v WHERE v.domain_id = :domain AND v.branch = ANY(CAST(:branches AS text[])) "
    "AND v.records > 0 AND v.norm % lb_value_norm(:term) "
    "AND (CAST(:kinds AS text[]) IS NULL OR v.kind_id = ANY(CAST(:kinds AS text[]))) "
    "GROUP BY v.kind_id, v.attribute_id, v.value ORDER BY 6 DESC, 5 DESC, v.value LIMIT :limit"
)


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
    kind_ids: Sequence[str] | None = None,
    limit: int,
) -> list[ValueHit]:
    """Values whose compared form equals that of a term, at most `limit` in all (by term,
    most held first)."""
    if not terms:
        return []
    params = {
        "domain": layout.domain_id,
        "branches": _branches(layout),
        "terms": list(terms),
        "kinds": None if kind_ids is None else list(kind_ids),
        "limit": limit,
    }
    return _hits((await session.execute(_EXACT, params)).all())


async def similar_values(
    session: AsyncSession,
    layout: BranchLayout,
    terms: Sequence[str],
    *,
    threshold: float,
    per_term: int,
    kind_ids: Sequence[str] | None = None,
    runtime: StoreRuntime | None = None,
) -> list[ValueHit]:
    """For each term, its `per_term` most similar values (trigram similarity of the compared
    forms, at least `threshold`), best first."""
    settings = (runtime or default_runtime()).settings
    kinds = None if kind_ids is None else list(kind_ids)
    found: list[ValueHit] = []
    if settings.reads.use_definer_functions:
        per_term = min(per_term, settings.reads.definer_ceilings.limit)
        for start in range(0, len(terms), MAX_TERMS):
            record_definer_call("lb_values_similar")
            params = {
                "domain": layout.domain_id,
                "branch": layout.branch,
                "kinds": kinds,
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
            "kinds": kinds,
            "term": term,
            "limit": per_term,
        }
        found.extend(_hits((await session.execute(_SIMILAR_SQL, params)).all()))
    return found


_INDEXED_COUNTS = text(
    "SELECT value, records FROM mat_values WHERE domain_id = :domain AND branch = :branch "
    "AND kind_id = :kind AND attribute_id = :attribute AND value = ANY(CAST(:wanted AS text[]))"
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
