"""Assembly of the health reports from the scans.

A report covers the Kinds the caller may read (`KindScope`) and the Relations whose both ends
are such Kinds, so nothing about a Kind the caller cannot see is counted or listed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Kind, Relation
from lorebase_keeper.errors import UNAVAILABLE, ProblemError
from lorebase_keeper.kindhealth import scan
from lorebase_keeper.kindhealth.config import HealthSettings
from lorebase_keeper.kindhealth.models import HealthSummary, KindHealth, RelationHealth
from lorebase_keeper.reading.points import ReadPoint


@dataclass(frozen=True, slots=True)
class Findings:
    """What a scan found: Kind reports, Relation reports and the totals over them."""

    kinds: list[KindHealth]
    relations: list[RelationHealth]
    summary: HealthSummary


def visible_kinds(point: ReadPoint) -> list[Kind]:
    """The Kinds the caller may read, by api name."""
    kinds = [k for k in point.schema.kinds.values() if point.scope.allows(k.api_name)]
    return sorted(kinds, key=lambda k: k.api_name)


def visible_relations(point: ReadPoint, kind: Kind | None = None) -> list[Relation]:
    """Relations between Kinds the caller may read; only those of `kind` when given."""
    schema = point.schema
    found: list[Relation] = []
    for relation in schema.relations.values():
        ends = [schema.kinds.get(relation.from_kind), schema.kinds.get(relation.to_kind)]
        if any(end is None or not point.scope.allows(end.api_name) for end in ends):
            continue
        if kind is None or kind.id in (relation.from_kind, relation.to_kind):
            found.append(relation)
    return sorted(found, key=lambda r: r.api_name)


async def scan_branch(
    session: AsyncSession,
    point: ReadPoint,
    settings: HealthSettings,
    kinds: Sequence[Kind],
    relations: Sequence[Relation],
) -> Findings:
    """Run every check for `kinds` and `relations` at the head of the point's branch."""
    where = scan.Branch(point.domain.id, point.branch)
    schema = point.schema
    await session.execute(
        select(func.set_config("statement_timeout", str(settings.statement_timeout_ms), True))
    )
    try:
        counts = await scan.record_counts(session, where)
        stored_links = await scan.link_counts(session, where)
        changes = await scan.last_changes(session, where, [k.id for k in kinds])
        reports: list[KindHealth] = []
        for kind in kinds:
            total = counts.get(kind.id, 0)
            reports.append(
                KindHealth(
                    kind=kind.api_name,
                    records=total,
                    last_change=changes.get(kind.id),
                    violations=await scan.validate_records(
                        session, where, schema, kind, total, settings
                    ),
                    duplicates=await scan.find_duplicates(session, where, kind, total, settings),
                    derived=await scan.derived_status(session, where, kind, total),
                )
            )
        related = [
            await scan.relation_health(session, where, schema, relation, stored_links, settings)
            for relation in relations
        ]
    except DBAPIError as error:
        if scan.is_timeout(error):
            raise ProblemError(
                UNAVAILABLE,
                "The health scan took longer than read.health.statement_timeout_ms allows. "
                "Ask for a single Kind, or raise the limit.",
            ) from None
        raise
    return Findings(reports, related, summarize(reports, related))


def summarize(kinds: Sequence[KindHealth], relations: Sequence[RelationHealth]) -> HealthSummary:
    """Totals over Kind and Relation reports."""
    invalid = sum(k.violations.invalid_records for k in kinds)
    orphans = sum(r.orphan_links.count for r in relations)
    gaps = sum(r.required_gaps.missing for r in relations if r.required_gaps is not None)
    duplicates = sum(k.duplicates.total_groups for k in kinds)
    unique = sum(1 for k in kinds for g in k.duplicates.groups if g.unique)
    return HealthSummary(
        ok=not (invalid or orphans or gaps or unique),
        kinds=len(kinds),
        records=sum(k.records for k in kinds),
        links=sum(r.links for r in relations),
        invalid_records=invalid,
        orphan_links=orphans,
        required_relation_gaps=gaps,
        duplicate_groups=duplicates,
        stale_kinds=sum(1 for k in kinds if k.derived is not None and k.derived.stale),
        sampled_kinds=sum(1 for k in kinds if k.violations.sampled),
    )


def with_direction(relations: Sequence[RelationHealth], kind: Kind) -> list[RelationHealth]:
    """The Relation reports of a Kind report, each marked with the side the Kind is on."""
    out: list[RelationHealth] = []
    for item in relations:
        outgoing = item.from_kind == kind.api_name
        incoming = item.to_kind == kind.api_name
        side = "self" if outgoing and incoming else "outgoing" if outgoing else "incoming"
        out.append(item.model_copy(update={"direction": side}))
    return out
