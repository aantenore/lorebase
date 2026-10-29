"""Items of extraction Proposals (design 7.3, 9.2 step 11): one proposed Record, Attribute value
or Link each, with evidence, confidence, match details and the reviewer's decision.

`subject` holds what the item proposes, with Attribute values in canonical form:

- `record`: `{"kind", "key", "values": {attribute id: value}, "evidence": {attribute id:
  evidence}}`, a new Record;
- `value`: `{"kind", "key", "attribute", "value", "current"}`, a value of an existing Record;
- `link`: `{"relation", "from", "to", "values"}`.

`evidence` is the primary evidence, `{"document_id", "chunk", "start", "end", "quote"}` with
offsets into the parsed document text. `edits` holds the reviewer's changes, applied when the
item is accepted.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from sqlalchemy import Table, func, insert, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.models import metadata
from lorebase_store.models.enums import ItemDecision
from lorebase_store.repositories.quotes import erase_quotes

ITEMS: Table = metadata.tables["proposal_items"]

RECORD_ITEM = "record"
VALUE_ITEM = "value"
LINK_ITEM = "link"
ITEM_TYPES = (RECORD_ITEM, VALUE_ITEM, LINK_ITEM)


@dataclass(frozen=True, slots=True)
class ItemRow:
    item_id: str
    item_type: str
    seq: int
    subject: dict[str, Any]
    partition: str
    element_key: str
    attribute_id: str | None = None
    evidence: dict[str, Any] | None = None
    confidence: float | None = None
    decision: ItemDecision = ItemDecision.PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    job_id: str | None = None
    match: dict[str, Any] | None = None
    tier: int | None = None
    model: str | None = None
    edits: dict[str, Any] | None = None
    created_at: datetime | None = None

    @property
    def target(self) -> str:
        """The Kind id of a Record or value item, the Relation id of a Link item."""
        return str(self.subject.get("relation") or self.subject.get("kind") or "")


@dataclass(frozen=True, slots=True)
class ItemFilter:
    """Optional filters of an item listing; `targets` are Kind or Relation ids."""

    decisions: tuple[ItemDecision, ...] = ()
    targets: tuple[str, ...] = ()
    item_types: tuple[str, ...] = ()
    min_confidence: float | None = None
    max_confidence: float | None = None
    item_ids: tuple[str, ...] = ()


_COLUMNS = tuple(f.name for f in fields(ItemRow))


def _item(found: Any) -> ItemRow:
    return ItemRow(**{name: found[name] for name in _COLUMNS})


class ProposalItemRepository:
    """Items of the Proposals of one Domain, in the caller's transaction."""

    def __init__(self, session: AsyncSession, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id

    async def insert(self, proposal_id: str, rows: Sequence[ItemRow]) -> None:
        if not rows:
            return
        await self._session.execute(
            insert(ITEMS),
            [
                {
                    "domain_id": self._domain,
                    "proposal_id": proposal_id,
                    **{
                        name: getattr(row, name)
                        for name in _COLUMNS
                        if not (name == "created_at" and row.created_at is None)
                    },
                }
                for row in rows
            ],
        )

    def _where(self, proposal_id: str, filters: ItemFilter) -> list[Any]:
        table = ITEMS
        conditions: list[Any] = [
            table.c.domain_id == self._domain,
            table.c.proposal_id == proposal_id,
        ]
        if filters.decisions:
            conditions.append(table.c.decision.in_(filters.decisions))
        if filters.item_types:
            conditions.append(table.c.item_type.in_(filters.item_types))
        if filters.targets:
            targets = list(filters.targets)
            conditions.append(
                or_(
                    table.c.subject["kind"].astext.in_(targets),
                    table.c.subject["relation"].astext.in_(targets),
                )
            )
        if filters.min_confidence is not None:
            conditions.append(table.c.confidence >= filters.min_confidence)
        if filters.max_confidence is not None:
            conditions.append(table.c.confidence < filters.max_confidence)
        if filters.item_ids:
            conditions.append(table.c.item_id.in_(filters.item_ids))
        return conditions

    async def page(
        self,
        proposal_id: str,
        filters: ItemFilter,
        *,
        limit: int,
        after: tuple[int, str] | None = None,
    ) -> list[ItemRow]:
        """Items in proposal order, after the keyset `(seq, item_id)`."""
        table = ITEMS
        query = select(table).where(*self._where(proposal_id, filters))
        if after is not None:
            seq, last = after
            query = query.where(
                or_(table.c.seq > seq, (table.c.seq == seq) & (table.c.item_id > last))
            )
        query = query.order_by(table.c.seq, table.c.item_id).limit(limit)
        return [_item(row) for row in (await self._session.execute(query)).mappings()]

    async def all(
        self,
        proposal_id: str,
        filters: ItemFilter | None = None,
        *,
        for_update: bool = False,
    ) -> list[ItemRow]:
        """Every item matching `filters`, in proposal order."""
        table = ITEMS
        query = (
            select(table)
            .where(*self._where(proposal_id, filters or ItemFilter()))
            .order_by(table.c.seq, table.c.item_id)
        )
        if for_update:
            query = query.with_for_update()
        return [_item(row) for row in (await self._session.execute(query)).mappings()]

    async def decide(
        self,
        proposal_id: str,
        item_id: str,
        *,
        decision: ItemDecision,
        decided_by: str | None,
        decided_at: datetime | None,
        edits: dict[str, Any] | None,
    ) -> None:
        await self._session.execute(
            update(ITEMS)
            .where(
                ITEMS.c.domain_id == self._domain,
                ITEMS.c.proposal_id == proposal_id,
                ITEMS.c.item_id == item_id,
            )
            .values(decision=decision, decided_by=decided_by, decided_at=decided_at, edits=edits)
        )

    async def decisions(
        self, refs: Iterable[tuple[str, str]]
    ) -> dict[tuple[str, str], ItemDecision]:
        """The decision of each `(proposal_id, item_id)` of `refs` that exists, in one query."""
        wanted = sorted(set(refs))
        if not wanted:
            return {}
        result = await self._session.execute(
            select(ITEMS.c.proposal_id, ITEMS.c.item_id, ITEMS.c.decision).where(
                ITEMS.c.domain_id == self._domain,
                tuple_(ITEMS.c.proposal_id, ITEMS.c.item_id).in_(wanted),
            )
        )
        return {
            (str(proposal), str(item)): ItemDecision(decision)
            for proposal, item, decision in result
        }

    async def counts(self, proposal_id: str) -> dict[str, int]:
        """Number of items per decision."""
        result = await self._session.execute(
            select(ITEMS.c.decision, func.count())
            .where(ITEMS.c.domain_id == self._domain, ITEMS.c.proposal_id == proposal_id)
            .group_by(ITEMS.c.decision)
        )
        return {ItemDecision(decision).value: int(count) for decision, count in result}

    async def of_jobs(
        self,
        job_ids: Iterable[str],
        *,
        decisions: Iterable[ItemDecision] = (),
        item_types: Iterable[str] = (),
    ) -> list[ItemRow]:
        """Items produced by the extraction jobs `job_ids`, in job then proposal order."""
        wanted = sorted(set(job_ids))
        if not wanted:
            return []
        query = select(ITEMS).where(ITEMS.c.domain_id == self._domain, ITEMS.c.job_id.in_(wanted))
        chosen = list(decisions)
        if chosen:
            query = query.where(ITEMS.c.decision.in_(chosen))
        types = list(item_types)
        if types:
            query = query.where(ITEMS.c.item_type.in_(types))
        query = query.order_by(ITEMS.c.job_id, ITEMS.c.seq, ITEMS.c.item_id)
        return [_item(row) for row in (await self._session.execute(query)).mappings()]

    async def erase_quotes(self, job_ids: Iterable[str], document_id: str, erased: str) -> int:
        """Replace the evidence quotes of `document_id` in the items of `job_ids` (subject and
        evidence) by `erased` (document purge); the number of items changed."""
        changed = 0
        for row in await self.of_jobs(job_ids):
            subject, in_subject = erase_quotes(row.subject, document_id, erased)
            evidence, in_evidence = erase_quotes(row.evidence, document_id, erased)
            if not (in_subject or in_evidence):
                continue
            await self._session.execute(
                update(ITEMS)
                .where(
                    ITEMS.c.domain_id == self._domain,
                    ITEMS.c.job_id == row.job_id,
                    ITEMS.c.item_id == row.item_id,
                )
                .values(subject=subject, evidence=evidence)
            )
            changed += 1
        return changed
