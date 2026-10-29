"""Duplicate Records for reconciliation (design 10.6, stage a).

Two rules, both reading one branch and capped at `duplicates.max_pairs_per_kind` pairs per Kind
and pass:

- `normalized_pairs`: Records equal on a unique or title Attribute once case and spacing are
  folded (`lorebase_store.duplicates`, the rule of the health report);
- `trigram_pairs`: Records whose title is at least `duplicates.trigram_threshold` similar to the
  title of a given Record (`lorebase_store.reads.similar_titles`), one statement per Record.

A pair only raises a `duplicate` candidate: no claim moves and no Record is merged.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Kind
from lorebase_core.reconcile import DuplicateRule, duplicate_draft, pairs
from lorebase_scribe.reconcile.detector import Raised
from lorebase_store.duplicates import duplicate_attributes, duplicate_groups
from lorebase_store.layout import BranchLayout
from lorebase_store.reads import similar_titles
from lorebase_store.runtime import StoreRuntime


async def normalized_pairs(
    session: AsyncSession,
    layout: BranchLayout,
    kind: Kind,
    *,
    branch: str,
    limit: int,
    involving: Collection[str] | None = None,
    proposal_id: str | None = None,
) -> list[Raised]:
    """Pairs of `kind` sharing a folded unique or title value on `layout`, raised on `branch`;
    with `involving`, only pairs with one of those keys."""
    raised: dict[tuple[str, str], Raised] = {}
    for attribute_id in duplicate_attributes(kind):
        attribute = kind.attributes[attribute_id]
        if attribute.in_vault:
            continue
        groups = await duplicate_groups(
            session, layout, kind.id, attribute_id, keys_per_group=limit, max_groups=limit
        )
        for group in groups:
            for pair in pairs(group.member_keys, limit=limit):
                if len(raised) >= limit:
                    return list(raised.values())
                if involving is not None and not set(pair) & set(involving):
                    continue
                raised.setdefault(
                    pair,
                    Raised(
                        duplicate_draft(
                            branch=branch,
                            kind_id=kind.id,
                            keys=pair,
                            rule=DuplicateRule.NORMALIZED,
                            attribute_id=attribute_id,
                            proposal_id=proposal_id,
                        )
                    ),
                )
    return list(raised.values())


async def trigram_pairs(
    session: AsyncSession,
    runtime: StoreRuntime,
    kind: Kind,
    records: Iterable[tuple[str, str | None]],
    *,
    domain_id: str,
    read_branch: str,
    branch: str,
    threshold: float,
    limit: int,
    proposal_id: str | None = None,
) -> tuple[list[Raised], int]:
    """Pairs of each `(key, title)` with the Records of `kind` on `read_branch` whose title is
    similar (module doc), raised on `branch`; also how many statements ran."""
    raised: dict[tuple[str, str], Raised] = {}
    statements = 0
    for key, title in records:
        if len(raised) >= limit:
            break
        if not title or not title.strip():
            continue
        statements += 1
        hits = await similar_titles(
            session,
            domain_id,
            read_branch,
            title,
            kind_ids=[kind.id],
            threshold=threshold,
            limit=limit - len(raised) + 1,
            runtime=runtime,
        )
        for hit in hits:
            if hit.key == key or len(raised) >= limit:
                continue
            pair = (min(key, hit.key), max(key, hit.key))
            raised.setdefault(
                pair,
                Raised(
                    duplicate_draft(
                        branch=branch,
                        kind_id=kind.id,
                        keys=pair,
                        rule=DuplicateRule.TRIGRAM,
                        score=hit.score,
                        proposal_id=proposal_id,
                    )
                ),
            )
    return list(raised.values()), statements


__all__ = ["normalized_pairs", "trigram_pairs"]
