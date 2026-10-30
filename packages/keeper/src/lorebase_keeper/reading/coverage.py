"""The `coverage` block of queries and resolutions (design 8.11): how complete an answer can
be, so that "no result" is told apart from "missing data".

For each Attribute a request used (in `where`, `select` or `order_by`, or named by a
resolution candidate) the share of the Kind's Records without a value; for each Kind read,
the Sources that feed it, when they last synchronized and whether that is older than their
`stale_after`; for a resolution, the phrases that matched nothing. `notes` says each signal
in one line.

**Cost.** The block reads statistics, never Records: one primary key lookup of `mat_coverage`
for the Kinds read and, when Sources feed them, one index probe of `source_runs` per Source
(`lorebase_store.coverage`). Scribe counts the statistics per default branch head.

**What a caller sees.** Only Kinds in the read point's scope and Attributes whose values the
caller may read (`Masking.hidden`): a masked Attribute is left out of the block, as from
resolution. Counts are per Kind: no read restricts rows within a Kind (row-level security
isolates Domains, grants narrow to Kinds and branches), so a count tells nothing a
`lorebase_query` aggregate would not. Statistics exist for the default branch head only: on
another branch or at another Revision the block keeps the unknown phrases and says why the
rest is omitted, since a grant narrowed to one branch must not see the default branch's
counts. Statistics counted at an older head are marked `approximate`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import ElementStatus, Kind
from lorebase_core.query import FieldRef, FieldSource, QueryPlan
from lorebase_keeper.reading.points import ReadPoint, ServedFrom
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.reading.sensitivity import condition_fields
from lorebase_keeper.reading.views import (
    AttributeCoverage,
    CoverageOut,
    KindCoverage,
    SourceFreshness,
)
from lorebase_store import coverage as statistics

Use = Literal["where", "select", "order_by", "resolve"]
USES: tuple[Use, ...] = ("where", "order_by", "select", "resolve")
"""Uses in the order notes name them (the first one a note describes)."""

NOT_DEFAULT = "statistics cover the default branch head only"


def wanted(requested: bool | None, runtime: ReadRuntime) -> bool:
    """Whether a request gets the block: its own `coverage`, else the setting."""
    return runtime.read.coverage.enabled if requested is None else requested


def plan_uses(plan: QueryPlan) -> dict[tuple[str, str], set[Use]]:
    """(Kind id, Attribute id) -> how a query uses it: the fields of its target Kinds compared
    in `where` (not through Relation predicates), selected and ordered by."""
    kinds = plan.target.kinds
    uses: dict[tuple[str, str], set[Use]] = defaultdict(set)

    def add(ref: FieldRef, use: Use) -> None:
        if ref.source is not FieldSource.VALUE:
            return
        for kind_id, attribute_id in ref.attributes.items():
            uses[(kind_id, attribute_id)].add(use)

    target = {k.id for k in kinds}
    for ref, over in condition_fields(plan.where, kinds):
        if {k.id for k in over} <= target:
            add(ref, "where")
    for ordering in plan.order:
        add(ordering.field, "order_by")
    for ref in plan.select or ():
        add(ref, "select")
    return uses


def _share(missing: int, records: int) -> float:
    return round(missing / records, 4) if records else 0.0


def _percent(share: float) -> str:
    return "<1%" if 0 < share < 0.01 else f"{share:.0%}"


def _note(item: AttributeCoverage, approximate: bool) -> str:
    head = f"{item.attribute} missing on {_percent(item.missing)} of {item.kind}"
    if approximate:
        head += " (approximate)"
    use = next(u for u in USES if u in item.used_in)
    if use in ("where", "resolve"):
        return f"{head}: a filter on {item.attribute} excludes them"
    if use == "order_by":
        return f"{head}: ordering by {item.attribute} cannot place them by value"
    return f"{head}: they have no {item.attribute} to show"


def _source_note(item: SourceFreshness) -> list[str]:
    notes: list[str] = []
    if item.stale:
        when = (
            "never synchronized"
            if item.last_synced_at is None
            else f"last synchronized {item.last_synced_at.isoformat(timespec='seconds')}"
        )
        notes.append(
            f"{item.kind} from Source {item.source}: {when}, older than stale_after "
            f"{item.stale_after}"
        )
    if item.unseen_records:
        notes.append(
            f"{item.unseen_records} {item.kind} Records not seen by Source {item.source} "
            f"for {item.stale_after}"
        )
    return notes


def unknown_notes(unknown: Iterable[str]) -> list[str]:
    return [f"'{p}' matched nothing in this Domain (no Kind, Attribute or value)" for p in unknown]


async def build(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    kinds: Sequence[Kind],
    uses: Mapping[tuple[str, str], set[Use]],
    *,
    unknown: Sequence[str] = (),
    now: datetime | None = None,
) -> CoverageOut:
    """The block for a request reading `kinds` and using the Attributes of `uses`."""
    out = CoverageOut(unknown=list(unknown), notes=unknown_notes(unknown))
    readable = [k for k in kinds if point.scope.allows(k.api_name)]
    if not readable:
        return out
    if (
        point.branch != point.domain.default_branch
        or point.served_from is not ServedFrom.MATERIALIZED
        or point.snapshot
    ):
        out.omitted = NOT_DEFAULT
        return out
    stats = await statistics.read_coverage(
        session, point.domain.id, point.branch, [k.id for k in readable]
    )
    settings = runtime.read.coverage
    for kind in readable:
        found = stats.get(kind.id)
        if found is None:
            continue
        approximate = found.sampled or found.revision != point.revision.id
        out.kinds.append(
            KindCoverage(
                kind=kind.api_name,
                records=found.records,
                as_of=found.revision.hex(),
                computed_at=found.computed_at,
                approximate=approximate,
            )
        )
        for (kind_id, attribute_id), used in sorted(uses.items()):
            attribute = kind.attributes.get(attribute_id)
            if kind_id != kind.id or attribute is None:
                continue
            if point.masking.hidden(attribute, [kind.id]) or attribute_id not in found.missing:
                continue
            missing = found.missing[attribute_id]
            item = AttributeCoverage(
                kind=kind.api_name,
                attribute=attribute.api_name,
                missing=_share(missing, found.records),
                missing_records=missing,
                used_in=[u for u in USES if u in used],
            )
            out.attributes.append(item)
            if item.missing > 0 and item.missing >= settings.note_min_share:
                out.notes.append(_note(item, approximate))
    if len(out.kinds) < len(readable):
        out.omitted = "statistics not counted yet for some Kinds"
    await _sources(session, point, readable, stats, out, now or datetime.now(UTC))
    return out


async def _sources(
    session: AsyncSession,
    point: ReadPoint,
    kinds: Sequence[Kind],
    stats: Mapping[str, statistics.KindCoverage],
    out: CoverageOut,
    now: datetime,
) -> None:
    by_kind = {k.id: k for k in kinds}
    feeding = [
        s
        for s in point.schema.sources.values()
        if s.target_kind in by_kind and s.status is not ElementStatus.DEPRECATED
    ]
    if not feeding:
        return
    last = await statistics.last_runs(session, point.domain.id, [s.id for s in feeding])
    for source in sorted(feeding, key=lambda s: s.api_name):
        kind = by_kind[source.target_kind]
        found = stats.get(kind.id)
        synced = last.get(source.id)
        item = SourceFreshness(
            kind=kind.api_name,
            source=source.api_name,
            last_synced_at=synced,
            stale_after=source.stale_after,
            stale=statistics.stale(synced, source.stale_after_delta, now),
            unseen_records=None if found is None else found.unseen.get(source.id),
        )
        out.sources.append(item)
        out.notes.extend(_source_note(item))


async def for_query(
    session: AsyncSession, runtime: ReadRuntime, point: ReadPoint, plan: QueryPlan
) -> CoverageOut:
    return await build(session, runtime, point, plan.target.kinds, plan_uses(plan))


async def for_resolution(
    session: AsyncSession,
    runtime: ReadRuntime,
    point: ReadPoint,
    kinds: Sequence[Kind],
    attributes: Iterable[tuple[str, str]],
    unknown: Sequence[str],
) -> CoverageOut:
    """The block of a resolution: the unknown phrases, the Kinds of the candidates and the
    Attributes they named."""
    uses: dict[tuple[str, str], set[Use]] = {pair: {"resolve"} for pair in attributes}
    return await build(session, runtime, point, kinds, uses, unknown=unknown)
