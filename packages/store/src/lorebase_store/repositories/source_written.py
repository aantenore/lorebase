"""The last-written state of Sources (design 9.1 "Corrections that stick", 7.4).

`source_written` holds what each Source last wrote to a target branch, per element (a Kind
id for Records, a Relation id for Links) and key: the hash of each value, never the value.
`source_written_pending` holds the state a run carries in its open Proposal; `promote` moves
it to `source_written` in the transaction that merges the Proposal, so the state advances only
with a merge. Rows are bookkeeping for syncs: they are not content and not provenance.

`last_seen_run` and `last_seen_at` record the run that last read an element upstream (`seen`,
incremental Sources) apart from the merge-gated state: an element read and unchanged advances
no state but is still seen. A state advance counts as seen by the run that staged it; `unseen`
pages the Records not seen since a time (`updated_at` standing in while nothing was recorded).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Table, delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.bulk import OnConflict, bulk_write, chunks
from lorebase_store.models import metadata
from lorebase_store.settings import WriteSettings

WRITTEN: Table = metadata.tables["source_written"]
PENDING: Table = metadata.tables["source_written_pending"]

#: Columns an upsert replaces; `updated_at` takes its default (`now()`).
_STATE_COLUMNS = ("written", "revision_id", "value_revisions", "deleted", "run_id", "updated_at")
#: Columns `record` also replaces: the run recording the state saw the element.
_SEEN_COLUMNS = ("last_seen_run", "last_seen_at")

_PROMOTE = text(
    """
    INSERT INTO source_written AS w
        (domain_id, source_id, target_branch, element, key, written, revision_id,
         value_revisions, deleted, run_id, updated_at, last_seen_run, last_seen_at)
    SELECT domain_id, source_id, target_branch, element, key, written, revision_id,
           value_revisions, deleted, run_id, now(), run_id, updated_at
    FROM source_written_pending
    WHERE domain_id = :domain AND proposal_id = :proposal
    ON CONFLICT (domain_id, source_id, target_branch, element, key) DO UPDATE SET
        written = excluded.written, revision_id = excluded.revision_id,
        value_revisions = excluded.value_revisions, deleted = excluded.deleted,
        run_id = excluded.run_id, updated_at = excluded.updated_at,
        last_seen_run = CASE WHEN w.last_seen_at IS NULL OR w.last_seen_at < excluded.last_seen_at
            THEN excluded.last_seen_run ELSE w.last_seen_run END,
        last_seen_at = GREATEST(w.last_seen_at, excluded.last_seen_at)
    """
)


@dataclass(frozen=True, slots=True)
class WrittenRow:
    """The state of one element.

    `written` maps Attribute ids to value hashes (hex; None: absent); `revision_id` is the
    Source branch Revision holding those values, except for the Attributes of
    `value_revisions` (Attribute id to Revision hex), whose value an older Revision holds.
    `preserve` (pending rows only) lists the Attributes whose merge base keeps the
    last-written value.
    """

    element: str
    key: str
    written: Mapping[str, str | None]
    revision_id: bytes | None
    run_id: str
    value_revisions: Mapping[str, str] = field(default_factory=dict)
    deleted: bool = False
    preserve: tuple[str, ...] = ()
    #: The run that last read the element upstream and when (current rows only; None: not
    #: recorded, `updated_at` stands in).
    last_seen_run: str | None = None
    last_seen_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def seen_at(self) -> datetime | None:
        """When the element was last seen, or else last advanced."""
        return self.last_seen_at or self.updated_at

    def revision_of(self, attribute: str) -> bytes | None:
        """The Revision holding the last-written value of `attribute`."""
        found = self.value_revisions.get(attribute)
        if found is not None:
            return bytes.fromhex(found)
        return self.revision_id


@dataclass(frozen=True, slots=True)
class PreservedRow:
    """A pending element with Attributes to preserve, and the state it advances from."""

    pending: WrittenRow
    current: WrittenRow | None


def _row(found: Any) -> WrittenRow:
    return WrittenRow(
        element=found["element"],
        key=found["key"],
        written=dict(found["written"] or {}),
        revision_id=None if found["revision_id"] is None else bytes(found["revision_id"]),
        run_id=found["run_id"],
        value_revisions=dict(found["value_revisions"] or {}),
        deleted=bool(found["deleted"]),
        preserve=tuple(found.get("preserve") or ()),
        last_seen_run=found.get("last_seen_run"),
        last_seen_at=found.get("last_seen_at"),
        updated_at=found.get("updated_at"),
    )


class SourceWrittenRepository:
    """Last-written state of one Domain, in the caller's transaction (after
    `scope_to_domain`)."""

    def __init__(self, session: AsyncSession, domain_id: str, *, writes: WriteSettings) -> None:
        self._session = session
        self._domain = domain_id
        self._writes = writes

    async def current(
        self,
        source_id: str,
        target_branch: str,
        element: str,
        keys: Iterable[str] | None = None,
    ) -> dict[str, WrittenRow]:
        """The state of `element` by key (every key when `keys` is None)."""
        where = [
            WRITTEN.c.domain_id == self._domain,
            WRITTEN.c.source_id == source_id,
            WRITTEN.c.target_branch == target_branch,
            WRITTEN.c.element == element,
        ]
        if keys is None:
            result = await self._session.execute(select(WRITTEN).where(*where))
            return {row.key: row for row in map(_row, result.mappings())}
        found: dict[str, WrittenRow] = {}
        for chunk in chunks(list(dict.fromkeys(keys)), self._writes.insert_batch_rows):
            result = await self._session.execute(
                select(WRITTEN).where(*where, WRITTEN.c.key.in_(chunk))
            )
            found.update((row.key, row) for row in map(_row, result.mappings()))
        return found

    async def pending(self, proposal_id: str, element: str) -> dict[str, WrittenRow]:
        """The state a Proposal carries for `element`, by key."""
        result = await self._session.execute(
            select(PENDING).where(
                PENDING.c.domain_id == self._domain,
                PENDING.c.proposal_id == proposal_id,
                PENDING.c.element == element,
            )
        )
        return {row.key: row for row in map(_row, result.mappings())}

    async def stage(
        self, proposal_id: str, source_id: str, target_branch: str, rows: Sequence[WrittenRow]
    ) -> None:
        """Carry `rows` in the Proposal, replacing what it carried for the same keys."""
        values = [
            {
                "domain_id": self._domain,
                "proposal_id": proposal_id,
                "element": row.element,
                "key": row.key,
                "source_id": source_id,
                "target_branch": target_branch,
                **self._state(row),
                "preserve": list(row.preserve),
            }
            for row in rows
        ]
        await bulk_write(
            self._session,
            PENDING,
            values,
            writes=self._writes,
            on_conflict=OnConflict.UPDATE,
            update_columns=(*_STATE_COLUMNS, "preserve"),
        )

    async def record(
        self,
        source_id: str,
        target_branch: str,
        rows: Sequence[WrittenRow],
        *,
        seen_at: datetime | None = None,
    ) -> None:
        """Write `rows` as the current state (a run whose state needs no merge); with
        `seen_at`, the run that recorded them also saw them then."""
        values = [
            {
                "domain_id": self._domain,
                "source_id": source_id,
                "target_branch": target_branch,
                "element": row.element,
                "key": row.key,
                **self._state(row),
                **(
                    {}
                    if seen_at is None
                    else {"last_seen_run": row.run_id, "last_seen_at": seen_at}
                ),
            }
            for row in rows
        ]
        await bulk_write(
            self._session,
            WRITTEN,
            values,
            writes=self._writes,
            on_conflict=OnConflict.UPDATE,
            update_columns=_STATE_COLUMNS if seen_at is None else (*_STATE_COLUMNS, *_SEEN_COLUMNS),
        )

    async def seen(
        self,
        source_id: str,
        target_branch: str,
        element: str,
        keys: Iterable[str],
        *,
        run_id: str,
        at: datetime,
    ) -> int:
        """Record that run `run_id` read `keys` of `element` upstream at `at` (elements with a
        state only; one never written is seen when its state advances). Returns the rows
        touched."""
        touched = 0
        for chunk in chunks(list(dict.fromkeys(keys)), self._writes.insert_batch_rows):
            result = await self._session.execute(
                update(WRITTEN)
                .where(
                    WRITTEN.c.domain_id == self._domain,
                    WRITTEN.c.source_id == source_id,
                    WRITTEN.c.target_branch == target_branch,
                    WRITTEN.c.element == element,
                    WRITTEN.c.key.in_(chunk),
                )
                .values(last_seen_run=run_id, last_seen_at=at)
            )
            touched += int(getattr(result, "rowcount", 0) or 0)
        return touched

    async def unseen(
        self,
        source_id: str,
        target_branch: str,
        element: str,
        *,
        before: datetime,
        after: str | None = None,
        limit: int,
    ) -> list[WrittenRow]:
        """Elements not deleted whose last sight (`last_seen_at`, else `updated_at`) is before
        `before`, in key order after `after`, at most `limit`."""
        seen = func.coalesce(WRITTEN.c.last_seen_at, WRITTEN.c.updated_at)
        query = (
            select(WRITTEN)
            .where(
                WRITTEN.c.domain_id == self._domain,
                WRITTEN.c.source_id == source_id,
                WRITTEN.c.target_branch == target_branch,
                WRITTEN.c.element == element,
                WRITTEN.c.deleted.is_(False),
                seen < before,
            )
            .order_by(WRITTEN.c.key)
            .limit(limit)
        )
        if after is not None:
            query = query.where(WRITTEN.c.key > after)
        result = await self._session.execute(query)
        return [_row(found) for found in result.mappings()]

    async def promote(self, proposal_id: str) -> int:
        """Move the state a merged Proposal carried to the current state; returns the number
        of elements advanced."""
        result = await self._session.execute(
            _PROMOTE, {"domain": self._domain, "proposal": proposal_id}
        )
        await self.discard(proposal_id)
        return int(getattr(result, "rowcount", 0) or 0)

    async def discard(self, proposal_id: str) -> int:
        """Drop the state a Proposal carries (it was merged or will never be)."""
        result = await self._session.execute(
            delete(PENDING).where(
                PENDING.c.domain_id == self._domain, PENDING.c.proposal_id == proposal_id
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def discard_others(self, source_id: str, target_branch: str, keep: str | None) -> int:
        """Drop the state of every other Proposal of a Source (closed without merging)."""
        where = [
            PENDING.c.domain_id == self._domain,
            PENDING.c.source_id == source_id,
            PENDING.c.target_branch == target_branch,
        ]
        if keep is not None:
            where.append(PENDING.c.proposal_id != keep)
        result = await self._session.execute(delete(PENDING).where(*where))
        return int(getattr(result, "rowcount", 0) or 0)

    async def preserved(self, proposal_id: str) -> list[PreservedRow]:
        """Pending elements of a Proposal with Attributes to preserve, with their current
        state (None when the Source never wrote them)."""
        result = await self._session.execute(
            select(PENDING).where(
                PENDING.c.domain_id == self._domain,
                PENDING.c.proposal_id == proposal_id,
                PENDING.c.preserve != text("'[]'::jsonb"),
            )
        )
        pending = [_row(found) for found in result.mappings()]
        if not pending:
            return []
        first = await self._session.execute(
            select(PENDING.c.source_id, PENDING.c.target_branch)
            .where(PENDING.c.domain_id == self._domain, PENDING.c.proposal_id == proposal_id)
            .limit(1)
        )
        source_id, target_branch = first.one()
        by_element: dict[str, list[str]] = {}
        for row in pending:
            by_element.setdefault(row.element, []).append(row.key)
        current: dict[tuple[str, str], WrittenRow] = {}
        for element, keys in by_element.items():
            found = await self.current(source_id, target_branch, element, keys)
            current.update(((element, key), row) for key, row in found.items())
        return [PreservedRow(row, current.get((row.element, row.key))) for row in pending]

    @staticmethod
    def _state(row: WrittenRow) -> dict[str, Any]:
        return {
            "written": dict(row.written),
            "revision_id": row.revision_id,
            "value_revisions": dict(row.value_revisions),
            "deleted": row.deleted,
            "run_id": row.run_id,
        }
