"""Records of one Kind that share a unique or title value (the duplicate rule).

Values are compared once trimmed, lower-cased and with runs of whitespace folded to one space;
empty values never match. Keeper's health report lists the groups (`lorebase_keeper.kindhealth`)
and Scribe's reconciliation raises `duplicate` candidates from them (design 10.6). The statement
reads the materialized head of one branch through its `BranchLayout` (an overlay through its
resolved rows), scoped to the Domain and the branch: a scan of the Kind at worst.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Kind
from lorebase_store.layout import BranchLayout

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


@dataclass(frozen=True, slots=True)
class DuplicateGroupRow:
    """Records sharing the normalized value `norm`: how many (`members`), the first
    `keys_per_group` keys in key order, and the totals over every group of the Attribute
    (`total_groups`, `total_members`), whatever the limit on groups."""

    norm: str
    members: int
    member_keys: list[str]
    total_groups: int
    total_members: int


def duplicate_attributes(kind: Kind) -> list[str]:
    """Attribute ids checked for duplicates: unique ones, then the title Attribute."""
    ids = [a.id for a in kind.attributes.values() if a.unique and not a.is_derived]
    title = kind.attributes.get(kind.title_attribute) if kind.title_attribute else None
    if title is not None and not title.is_derived and title.id not in ids:
        ids.append(title.id)
    return ids


async def duplicate_groups(
    session: AsyncSession,
    layout: BranchLayout,
    kind_id: str,
    attribute_id: str,
    *,
    keys_per_group: int,
    max_groups: int,
) -> list[DuplicateGroupRow]:
    """Groups of Records of `kind_id` sharing a value of `attribute_id` (module doc), largest
    first, at most `max_groups`."""
    rows = (
        await session.execute(
            text(_DUPLICATES.format(records=layout.records_from())),
            {
                "domain": layout.domain_id,
                "branch": layout.branch,
                "kind": kind_id,
                "attribute": attribute_id,
                "keys": keys_per_group,
                "groups": max_groups,
            },
        )
    ).all()
    return [
        DuplicateGroupRow(
            norm=row.norm,
            members=int(row.members),
            member_keys=list(row.member_keys),
            total_groups=int(row.total_groups),
            total_members=int(row.total_members),
        )
        for row in rows
    ]


__all__ = ["DuplicateGroupRow", "duplicate_attributes", "duplicate_groups"]
