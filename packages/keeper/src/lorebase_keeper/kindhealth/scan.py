"""Queries and checks behind the health reports, over the materialized head of one branch.

Each function answers one question for one Kind or Relation and reads the database through the
caller's session. Nothing here writes. Statements are scoped to the Domain and the branch and
use only primary-key friendly predicates, so the cost is a scan per Kind (validation, duplicates,
derived values) or per Relation (orphans and gaps) at worst. An overlay branch is read through
its resolved rows (`lorebase_store.layout`).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from sqlalchemy import (
    ColumnElement,
    FromClause,
    and_,
    exists,
    func,
    literal_column,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Attribute, Kind, Record, Relation, Schema
from lorebase_core.model.relation import RelationBacking
from lorebase_core.tree import kind_partition
from lorebase_core.validate import Issue, validate_record
from lorebase_keeper.kindhealth.config import HealthSettings
from lorebase_keeper.kindhealth.models import (
    DerivedReport,
    DuplicateGroup,
    DuplicateReport,
    HiddenDuplicates,
    IssueGroup,
    LastChange,
    LinkRef,
    OrphanLinks,
    RelationHealth,
    RequiredGaps,
    ViolationReport,
)
from lorebase_keeper.reading.views import kind_name
from lorebase_store.layout import BranchLayout

BATCH = 500
"""Records fetched per round trip while re-validating."""

_DUPLICATES = r"""
    WITH normalized AS (
        SELECT key, lower(regexp_replace(btrim("values" ->> :attribute), '\s+', ' ', 'g')) AS norm
        FROM {records}
        WHERE domain_id = :domain AND branch = :branch AND kind_id = :kind
          AND "values" ->> :attribute IS NOT NULL
    )
    SELECT norm, count(*) AS members,
           (array_agg(key ORDER BY key))[1:CAST(:keys AS int)] AS member_keys,
           count(*) OVER () AS total_groups,
           sum(count(*)) OVER () AS total_members
    FROM normalized
    WHERE norm <> ''
    GROUP BY norm
    HAVING count(*) > 1
    ORDER BY count(*) DESC, norm
    LIMIT :groups
    """


@dataclass(frozen=True)
class Branch:
    """The materialized head being examined; `layout` locates its rows (default: the
    branch's own rows)."""

    domain_id: str
    branch: str
    layout: BranchLayout | None = None

    @cached_property
    def rows(self) -> BranchLayout:
        return self.layout or BranchLayout(self.domain_id, self.branch)

    @cached_property
    def records(self) -> FromClause:
        return self.rows.records()

    @cached_property
    def links(self) -> FromClause:
        return self.rows.links()

    @cached_property
    def history(self) -> FromClause:
        return self.rows.history()


def _records(where: Branch) -> ColumnElement[bool]:
    r = where.records
    return and_(r.c.domain_id == where.domain_id, r.c.branch == where.branch)


def _links(where: Branch) -> ColumnElement[bool]:
    link = where.links
    return and_(link.c.domain_id == where.domain_id, link.c.branch == where.branch)


# Counts and history -------------------------------------------------------------------------


async def record_counts(session: AsyncSession, where: Branch) -> dict[str, int]:
    """Records per Kind id."""
    r = where.records
    result = await session.execute(
        select(r.c.kind_id, func.count()).where(_records(where)).group_by(r.c.kind_id)
    )
    return {kind_id: int(count) for kind_id, count in result}


async def link_counts(session: AsyncSession, where: Branch) -> dict[str, int]:
    """Stored Links per Relation id."""
    link = where.links
    result = await session.execute(
        select(link.c.relation_id, func.count()).where(_links(where)).group_by(link.c.relation_id)
    )
    return {relation_id: int(count) for relation_id, count in result}


async def last_changes(
    session: AsyncSession, where: Branch, kinds: Iterable[str]
) -> dict[str, LastChange]:
    """The latest Revision of the branch that touched each Kind (Record writes and deletions)."""
    partitions = {kind_partition(kind_id): kind_id for kind_id in kinds}
    if not partitions:
        return {}
    h = where.history
    result = await session.execute(
        select(h.c.partition, h.c.revision_id, h.c.at)
        .ext(distinct_on(h.c.partition))
        .where(
            h.c.domain_id == where.domain_id,
            h.c.branch == where.branch,
            h.c.partition.in_(list(partitions)),
        )
        .order_by(h.c.partition, h.c.at.desc(), h.c.revision_id)
    )
    return {
        partitions[partition]: LastChange(revision=bytes(revision).hex(), at=at)
        for partition, revision, at in result
    }


# Constraint violations ----------------------------------------------------------------------


def _sample_filter(step: int) -> ColumnElement[bool]:
    """Keeps one Record in `step`, chosen by a hash of its key: spread evenly and stable."""
    bits = "('x' || substr(md5(mat_records.key), 1, 7))::bit(28)::int"
    return literal_column(f"mod({bits}, {int(step)})") == 0


async def validate_records(
    session: AsyncSession,
    where: Branch,
    schema: Schema,
    kind: Kind,
    total: int,
    settings: HealthSettings,
    generalize: Callable[[list[Issue]], list[Issue]] | None = None,
) -> ViolationReport:
    """Re-validate the Records of a Kind with `validate_record` against the branch's schema.
    `generalize` rewrites the issues of each Record before they are grouped (issues about
    values the caller may not read lose their specific code)."""
    r = where.records
    step = max(1, math.ceil(total / settings.max_records_validated))
    checked = invalid = 0
    groups: dict[tuple[str, str | None], list[Any]] = defaultdict(lambda: [0, []])
    last: str | None = None
    while checked < settings.max_records_validated:
        query = (
            select(r.c.key, r.c["values"])
            .where(_records(where), r.c.kind_id == kind.id)
            .order_by(r.c.key)
            .limit(min(BATCH, settings.max_records_validated - checked))
        )
        if step > 1:
            query = query.where(_sample_filter(step))
        if last is not None:
            query = query.where(r.c.key > last)
        rows = (await session.execute(query)).all()
        if not rows:
            break
        for key, values in rows:
            checked += 1
            issues = validate_record(
                schema, Record.model_construct(kind=kind.id, key=key, values=values)
            )
            if generalize is not None:
                issues = generalize(issues)
            if issues:
                invalid += 1
            seen: set[tuple[str, str | None]] = set()
            for issue in issues:
                attribute = kind.attributes.get(issue.attribute) if issue.attribute else None
                name = attribute.api_name if attribute is not None else issue.attribute
                label = (issue.code.value, name)
                entry = groups[label]
                entry[0] += 1
                if label not in seen and len(entry[1]) < settings.examples_per_issue:
                    entry[1].append(key)
                seen.add(label)
        last = rows[-1][0]
    ordered = sorted(groups.items(), key=lambda item: (-item[1][0], item[0][0], item[0][1] or ""))
    return ViolationReport(
        checked=checked,
        sampled=step > 1,
        invalid_records=invalid,
        issues=[
            IssueGroup(code=code, attribute=name, count=count, examples=examples)
            for (code, name), (count, examples) in ordered[: settings.max_issue_groups]
        ],
        issues_truncated=len(ordered) > settings.max_issue_groups,
    )


# Duplicates ---------------------------------------------------------------------------------


def duplicate_attributes(kind: Kind) -> list[str]:
    """Attribute ids checked for duplicates: unique ones, then the title Attribute."""
    ids = [a.id for a in kind.attributes.values() if a.unique and not a.is_derived]
    title = kind.attributes.get(kind.title_attribute) if kind.title_attribute else None
    if title is not None and not title.is_derived and title.id not in ids:
        ids.append(title.id)
    return ids


async def find_duplicates(
    session: AsyncSession,
    where: Branch,
    kind: Kind,
    total: int,
    settings: HealthSettings,
    sensitive: Callable[[Attribute], bool] = lambda a: a.is_sensitive,
    hidden: Callable[[Attribute], bool] = lambda a: False,
) -> DuplicateReport:
    """Records of a Kind sharing a unique or title value, ignoring case and spacing.

    Groups of a `hidden` Attribute (one whose values the caller may not read) are not listed:
    which Records share a value would tell that their values are equal. Only how many groups
    and Records there are is reported for it."""
    ids = duplicate_attributes(kind)
    names = [kind.attributes[i].api_name for i in ids]
    if settings.duplicates_max_records == 0:
        return DuplicateReport(attributes=names, groups=[], total_groups=0, skipped="disabled")
    if total > settings.duplicates_max_records:
        return DuplicateReport(
            attributes=names, groups=[], total_groups=0, skipped="too_many_records"
        )
    found: list[DuplicateGroup] = []
    withheld: list[HiddenDuplicates] = []
    groups = 0
    for attribute_id in ids:
        attribute = kind.attributes[attribute_id]
        rows = (
            await session.execute(
                text(_DUPLICATES.format(records=where.rows.records_from())),
                {
                    "domain": where.domain_id,
                    "branch": where.branch,
                    "kind": kind.id,
                    "attribute": attribute_id,
                    "keys": settings.duplicate_keys_per_group,
                    "groups": settings.max_duplicate_groups,
                },
            )
        ).all()
        if rows:
            groups += int(rows[0].total_groups)
        if hidden(attribute):
            if rows:
                withheld.append(
                    HiddenDuplicates(
                        attribute=attribute.api_name,
                        unique=attribute.unique,
                        groups=int(rows[0].total_groups),
                        records=int(rows[0].total_members),
                    )
                )
            continue
        found.extend(
            DuplicateGroup(
                attribute=attribute.api_name,
                unique=attribute.unique,
                value=None if sensitive(attribute) else row.norm,
                count=int(row.members),
                keys=list(row.member_keys),
            )
            for row in rows
        )
    return DuplicateReport(attributes=names, groups=found, total_groups=groups, hidden=withheld)


# Derived values -----------------------------------------------------------------------------


async def derived_status(
    session: AsyncSession, where: Branch, kind: Kind, total: int
) -> DerivedReport | None:
    """Records lacking the materialized value of a derived Attribute (None: no such Attribute)."""
    derived = [a for a in kind.attributes.values() if a.is_derived]
    if not derived:
        return None
    r = where.records
    missing = 0
    if total:
        missing = int(
            (
                await session.execute(
                    select(func.count()).where(
                        _records(where),
                        r.c.kind_id == kind.id,
                        or_(*[~r.c.derived.has_key(a.id) for a in derived]),
                    )
                )
            ).scalar_one()
        )
    return DerivedReport(
        attributes=[a.api_name for a in derived], records_missing_value=missing, stale=missing > 0
    )


# Relations ----------------------------------------------------------------------------------


def _gaps(missing: int, keys: Iterable[str]) -> RequiredGaps:
    return RequiredGaps(missing=missing, examples=list(keys))


async def relation_health(
    session: AsyncSession,
    where: Branch,
    schema: Schema,
    relation: Relation,
    stored_links: Mapping[str, int],
    settings: HealthSettings,
) -> RelationHealth:
    """Links, orphan Links and required-relation gaps of a Relation."""
    if relation.backing is RelationBacking.FOREIGN_KEY and relation.foreign_key_attribute:
        links, orphans, gaps = await _foreign_key_health(session, where, relation, settings)
    else:
        links = stored_links.get(relation.id, 0)
        orphans = await _orphan_links(session, where, relation, settings)
        gaps = await _missing_links(session, where, relation, settings)
    return RelationHealth(
        relation=relation.api_name,
        from_kind=kind_name(schema, relation.from_kind),
        to_kind=kind_name(schema, relation.to_kind),
        backing=relation.backing,
        links=links,
        orphan_links=orphans,
        required=relation.required_from,
        required_gaps=gaps if relation.required_from else None,
    )


async def _orphan_links(
    session: AsyncSession, where: Branch, relation: Relation, settings: HealthSettings
) -> OrphanLinks:
    link = where.links
    f, t = where.rows.records("f"), where.rows.records("t")
    from_exists = exists().where(
        f.c.domain_id == link.c.domain_id,
        f.c.branch == link.c.branch,
        f.c.kind_id == relation.from_kind,
        f.c.key == link.c.from_key,
    )
    to_exists = exists().where(
        t.c.domain_id == link.c.domain_id,
        t.c.branch == link.c.branch,
        t.c.kind_id == relation.to_kind,
        t.c.key == link.c.to_key,
    )
    scope = (_links(where), link.c.relation_id == relation.id)
    counts = (
        await session.execute(
            select(
                func.count().filter(~from_exists),
                func.count().filter(~to_exists),
                func.count().filter(or_(~from_exists, ~to_exists)),
            ).where(*scope)
        )
    ).one()
    examples: list[LinkRef] = []
    if counts[2] and settings.orphan_examples:
        rows = await session.execute(
            select(link.c.from_key, link.c.to_key)
            .where(*scope, or_(~from_exists, ~to_exists))
            .order_by(link.c.from_key, link.c.to_key)
            .limit(settings.orphan_examples)
        )
        examples = [LinkRef(from_key=a, to_key=b) for a, b in rows]
    return OrphanLinks(
        count=int(counts[2]),
        missing_from=int(counts[0]),
        missing_to=int(counts[1]),
        examples=examples,
    )


async def _missing_links(
    session: AsyncSession, where: Branch, relation: Relation, settings: HealthSettings
) -> RequiredGaps:
    r, link = where.records, where.links
    linked = exists().where(
        link.c.domain_id == r.c.domain_id,
        link.c.branch == r.c.branch,
        link.c.relation_id == relation.id,
        link.c.from_key == r.c.key,
    )
    return await _gap_query(session, where, relation.from_kind, ~linked, settings)


async def _gap_query(
    session: AsyncSession,
    where: Branch,
    kind_id: str,
    condition: ColumnElement[bool],
    settings: HealthSettings,
) -> RequiredGaps:
    r = where.records
    scope = (_records(where), r.c.kind_id == kind_id, condition)
    missing = int((await session.execute(select(func.count()).where(*scope))).scalar_one())
    keys: Sequence[str] = []
    if missing and settings.orphan_examples:
        rows = await session.execute(
            select(r.c.key).where(*scope).order_by(r.c.key).limit(settings.orphan_examples)
        )
        keys = [key for (key,) in rows]
    return _gaps(missing, keys)


async def _foreign_key_health(
    session: AsyncSession, where: Branch, relation: Relation, settings: HealthSettings
) -> tuple[int, OrphanLinks, RequiredGaps]:
    """A foreign-key backed Relation has no stored Links: they are the `ref` values of the
    `from` Kind. Orphans are references to missing Records; gaps are Records without one."""
    r, t = where.records, where.rows.records("t")
    attribute = str(relation.foreign_key_attribute)
    value = r.c["values"][attribute].astext
    target_exists = exists().where(
        t.c.domain_id == r.c.domain_id,
        t.c.branch == r.c.branch,
        t.c.kind_id == relation.to_kind,
        t.c.key == value,
    )
    scope = (_records(where), r.c.kind_id == relation.from_kind)
    links = int(
        (await session.execute(select(func.count()).where(*scope, value.is_not(None)))).scalar_one()
    )
    dangling = (*scope, value.is_not(None), ~target_exists)
    count = int((await session.execute(select(func.count()).where(*dangling))).scalar_one())
    examples: list[LinkRef] = []
    if count and settings.orphan_examples:
        rows = await session.execute(
            select(r.c.key, value)
            .where(*dangling)
            .order_by(r.c.key)
            .limit(settings.orphan_examples)
        )
        examples = [LinkRef(from_key=a, to_key=b) for a, b in rows]
    orphans = OrphanLinks(count=count, missing_from=0, missing_to=count, examples=examples)
    gaps = await _gap_query(session, where, relation.from_kind, value.is_(None), settings)
    return links, orphans, gaps


def is_timeout(error: BaseException) -> bool:
    """Whether a database error is a statement timeout (SQLSTATE 57014)."""
    original = getattr(error, "orig", error)
    code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return code == "57014" or "statement timeout" in str(original)


__all__ = [
    "Branch",
    "derived_status",
    "duplicate_attributes",
    "find_duplicates",
    "is_timeout",
    "last_changes",
    "link_counts",
    "record_counts",
    "relation_health",
    "validate_records",
]
