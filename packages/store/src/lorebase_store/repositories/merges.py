"""Merge attempts, their Conflicts and the resolutions of a Proposal (design 6.8 step 5, 6.10).

A merge attempt records one run of the merge for a Proposal: base, ours (target head) and
theirs (source head), the outcome and the Conflicts. Conflicts are stored per attempt and
identified by their fingerprint (the public Conflict id is its hex form). Resolutions are
stored per Proposal by fingerprint, so they survive the recomputation of an attempt.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, Table, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.bulk import chunks
from lorebase_store.models import metadata
from lorebase_store.models.enums import MergeAttemptStatus

ATTEMPTS: Table = metadata.tables["merge_attempts"]
CONFLICTS: Table = metadata.tables["conflicts"]
RESOLUTIONS: Table = metadata.tables["conflict_resolutions"]

_INSERT_BATCH = 500
"""Rows per multi-row INSERT of Conflicts (each row carries a JSON payload)."""


@dataclass(frozen=True, slots=True)
class MergeAttemptRow:
    id: str
    proposal_id: str
    base: bytes | None
    ours: bytes
    theirs: bytes
    status: MergeAttemptStatus
    created_at: datetime
    virtual_base: bool = False
    result: str | None = None
    policy: dict[str, Any] | None = None
    created_by: str | None = None
    outcome: dict[str, Any] | None = None
    revision: bytes | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class ConflictRow:
    """A stored Conflict. `resolution` and `resolved_by` are set when the attempt applied a
    resolution to it (resolved); queries test `resolved_by`, which is SQL null otherwise."""

    fingerprint: str
    code: str
    partition: str
    element_key: str | None
    attribute_id: str | None
    payload: dict[str, Any]
    member_count: int
    resolution: dict[str, Any] | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None

    @property
    def resolved(self) -> bool:
        return self.resolved_by is not None


@dataclass(frozen=True, slots=True)
class ResolutionRow:
    fingerprint: str
    resolution: dict[str, Any]
    resolved_by: str
    at: datetime


def _attempt(found: Any) -> MergeAttemptRow:
    return MergeAttemptRow(**{k: v for k, v in found.items() if k != "domain_id"})


def _conflict(found: Any) -> ConflictRow:
    return ConflictRow(
        fingerprint=found["id"],
        code=found["code"],
        partition=found["partition"],
        element_key=found["element_key"],
        attribute_id=found["attribute_id"],
        payload=found["payload"],
        member_count=found["member_count"],
        resolution=found["resolution"],
        resolved_by=found["resolved_by"],
        resolved_at=found["resolved_at"],
    )


class MergeAttemptRepository:
    """Merge attempts of one Domain."""

    def __init__(self, session: AsyncSession, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id

    async def insert(self, row: MergeAttemptRow) -> None:
        await self._session.execute(
            insert(ATTEMPTS).values(
                domain_id=self._domain,
                id=row.id,
                proposal_id=row.proposal_id,
                base=row.base,
                ours=row.ours,
                theirs=row.theirs,
                status=row.status,
                created_at=row.created_at,
                virtual_base=row.virtual_base,
                result=row.result,
                policy=row.policy,
                created_by=row.created_by,
                outcome=row.outcome,
                revision=row.revision,
                idempotency_key=row.idempotency_key,
            )
        )

    async def get(self, attempt_id: str) -> MergeAttemptRow | None:
        result = await self._session.execute(
            select(ATTEMPTS).where(
                ATTEMPTS.c.domain_id == self._domain, ATTEMPTS.c.id == attempt_id
            )
        )
        found = result.mappings().one_or_none()
        return None if found is None else _attempt(found)

    async def latest(self, proposal_id: str) -> MergeAttemptRow | None:
        """The most recent attempt of a Proposal."""
        result = await self._session.execute(
            select(ATTEMPTS)
            .where(ATTEMPTS.c.domain_id == self._domain, ATTEMPTS.c.proposal_id == proposal_id)
            .order_by(ATTEMPTS.c.created_at.desc(), ATTEMPTS.c.id.desc())
            .limit(1)
        )
        found = result.mappings().one_or_none()
        return None if found is None else _attempt(found)

    async def latest_of(self, proposal_ids: Sequence[str]) -> dict[str, MergeAttemptRow]:
        """The most recent attempt of each of several Proposals."""
        if not proposal_ids:
            return {}
        result = await self._session.execute(
            select(ATTEMPTS)
            .distinct(ATTEMPTS.c.proposal_id)
            .where(
                ATTEMPTS.c.domain_id == self._domain,
                ATTEMPTS.c.proposal_id.in_(list(proposal_ids)),
            )
            .order_by(ATTEMPTS.c.proposal_id, ATTEMPTS.c.created_at.desc(), ATTEMPTS.c.id.desc())
        )
        return {row["proposal_id"]: _attempt(row) for row in result.mappings()}

    async def by_key(self, proposal_id: str, idempotency_key: str) -> MergeAttemptRow | None:
        """The attempt of a Proposal recorded under an idempotency key."""
        result = await self._session.execute(
            select(ATTEMPTS)
            .where(
                ATTEMPTS.c.domain_id == self._domain,
                ATTEMPTS.c.proposal_id == proposal_id,
                ATTEMPTS.c.idempotency_key == idempotency_key,
            )
            .order_by(ATTEMPTS.c.created_at.desc())
            .limit(1)
        )
        found = result.mappings().one_or_none()
        return None if found is None else _attempt(found)

    async def supersede(self, proposal_id: str, *, status: MergeAttemptStatus) -> int:
        """Set `status` (`stale` or `abandoned`) on the open attempts of a Proposal."""
        result = await self._session.execute(
            update(ATTEMPTS)
            .where(
                ATTEMPTS.c.domain_id == self._domain,
                ATTEMPTS.c.proposal_id == proposal_id,
                ATTEMPTS.c.status.in_([MergeAttemptStatus.CONFLICTED, MergeAttemptStatus.RESOLVED]),
            )
            .values(status=status)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def prune(self, proposal_id: str, *, keep: int) -> int:
        """Delete all but the `keep` most recent attempts of a Proposal (Conflicts cascade).

        Merged attempts are kept: they document how the Proposal was merged.
        """
        recent = (
            select(ATTEMPTS.c.id)
            .where(ATTEMPTS.c.domain_id == self._domain, ATTEMPTS.c.proposal_id == proposal_id)
            .order_by(ATTEMPTS.c.created_at.desc(), ATTEMPTS.c.id.desc())
            .limit(max(keep, 1))
        )
        result = await self._session.execute(
            delete(ATTEMPTS).where(
                ATTEMPTS.c.domain_id == self._domain,
                ATTEMPTS.c.proposal_id == proposal_id,
                ATTEMPTS.c.status != MergeAttemptStatus.MERGED,
                ATTEMPTS.c.id.not_in(recent.scalar_subquery()),
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def count(self, proposal_id: str) -> int:
        return int(
            await self._session.scalar(
                select(func.count())
                .select_from(ATTEMPTS)
                .where(ATTEMPTS.c.domain_id == self._domain, ATTEMPTS.c.proposal_id == proposal_id)
            )
            or 0
        )


@dataclass(frozen=True, slots=True)
class PartitionFilter:
    """The partitions whose Conflicts a reader sees: those in `include` (every partition when
    None) except those in `exclude`."""

    include: frozenset[str] | None = None
    exclude: frozenset[str] = frozenset()

    @property
    def everything(self) -> bool:
        return self.include is None and not self.exclude

    def allows(self, partition: str) -> bool:
        return (self.include is None or partition in self.include) and (
            partition not in self.exclude
        )

    def apply[T: Select[Any]](self, query: T) -> T:
        if self.include is not None:
            query = query.where(CONFLICTS.c.partition.in_(sorted(self.include)))
        if self.exclude:
            query = query.where(CONFLICTS.c.partition.not_in(sorted(self.exclude)))
        return query


EVERY_PARTITION = PartitionFilter()


class ConflictRepository:
    """Conflicts of the merge attempts of one Domain."""

    def __init__(self, session: AsyncSession, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id

    async def insert_many(self, attempt_id: str, rows: Iterable[ConflictRow]) -> None:
        values = [
            {
                "domain_id": self._domain,
                "attempt_id": attempt_id,
                "id": row.fingerprint,
                "code": row.code,
                "fingerprint": bytes.fromhex(row.fingerprint),
                "partition": row.partition,
                "element_key": row.element_key,
                "attribute_id": row.attribute_id,
                "payload": row.payload,
                "member_count": row.member_count,
                "resolution": row.resolution,
                "resolved_by": row.resolved_by,
                "resolved_at": row.resolved_at,
            }
            for row in rows
        ]
        for chunk in chunks(values, _INSERT_BATCH):
            await self._session.execute(insert(CONFLICTS), chunk)

    async def get(self, attempt_id: str, fingerprint: str) -> ConflictRow | None:
        result = await self._session.execute(
            select(CONFLICTS).where(
                CONFLICTS.c.domain_id == self._domain,
                CONFLICTS.c.attempt_id == attempt_id,
                CONFLICTS.c.id == fingerprint,
            )
        )
        found = result.mappings().one_or_none()
        return None if found is None else _conflict(found)

    async def page(
        self,
        attempt_id: str,
        *,
        codes: Sequence[str] = (),
        resolved: bool | None = None,
        limit: int,
        after: str | None = None,
        partitions: PartitionFilter = EVERY_PARTITION,
    ) -> list[ConflictRow]:
        """Conflicts of an attempt ordered by fingerprint, starting after `after`."""
        query = partitions.apply(
            select(CONFLICTS).where(
                CONFLICTS.c.domain_id == self._domain, CONFLICTS.c.attempt_id == attempt_id
            )
        )
        if codes:
            query = query.where(CONFLICTS.c.code.in_(list(codes)))
        if resolved is not None:
            query = query.where(
                CONFLICTS.c.resolved_by.is_not(None)
                if resolved
                else CONFLICTS.c.resolved_by.is_(None)
            )
        if after is not None:
            query = query.where(CONFLICTS.c.id > after)
        query = query.order_by(CONFLICTS.c.id).limit(limit)
        result = await self._session.execute(query)
        return [_conflict(row) for row in result.mappings()]

    async def counts(
        self, attempt_id: str, partitions: PartitionFilter = EVERY_PARTITION
    ) -> dict[tuple[str, bool], int]:
        """Number of Conflicts of an attempt per `(code, resolved)`."""
        resolved = CONFLICTS.c.resolved_by.is_not(None)
        query = partitions.apply(
            select(CONFLICTS.c.code, resolved, func.count()).where(
                CONFLICTS.c.domain_id == self._domain, CONFLICTS.c.attempt_id == attempt_id
            )
        )
        result = await self._session.execute(query.group_by(CONFLICTS.c.code, resolved))
        return {(code, bool(done)): int(count) for code, done, count in result}

    async def fingerprints(self, attempt_id: str, partitions: PartitionFilter) -> frozenset[str]:
        """Ids of the Conflicts of an attempt in the partitions `partitions` lets through."""
        query = partitions.apply(
            select(CONFLICTS.c.id).where(
                CONFLICTS.c.domain_id == self._domain, CONFLICTS.c.attempt_id == attempt_id
            )
        )
        return frozenset((await self._session.execute(query)).scalars())


class ResolutionRepository:
    """Resolutions of the Proposals of one Domain, by Conflict fingerprint."""

    def __init__(self, session: AsyncSession, domain_id: str) -> None:
        self._session = session
        self._domain = domain_id

    async def upsert_many(self, proposal_id: str, rows: Sequence[ResolutionRow]) -> None:
        """Store resolutions; a later resolution of the same fingerprint replaces the earlier."""
        if not rows:
            return
        latest = {row.fingerprint: row for row in rows}
        statement = pg_insert(RESOLUTIONS)
        statement = statement.on_conflict_do_update(
            index_elements=["domain_id", "proposal_id", "fingerprint"],
            set_={
                "resolution": statement.excluded.resolution,
                "resolved_by": statement.excluded.resolved_by,
                "at": statement.excluded.at,
            },
        )
        await self._session.execute(
            statement,
            [
                {
                    "domain_id": self._domain,
                    "proposal_id": proposal_id,
                    "fingerprint": bytes.fromhex(row.fingerprint),
                    "resolution": row.resolution,
                    "resolved_by": row.resolved_by,
                    "at": row.at,
                }
                for row in latest.values()
            ],
        )

    async def of(self, proposal_id: str) -> list[ResolutionRow]:
        """Resolutions of a Proposal ordered by time."""
        result = await self._session.execute(
            select(RESOLUTIONS)
            .where(
                RESOLUTIONS.c.domain_id == self._domain,
                RESOLUTIONS.c.proposal_id == proposal_id,
            )
            .order_by(RESOLUTIONS.c.at, RESOLUTIONS.c.fingerprint)
        )
        return [
            ResolutionRow(
                fingerprint=bytes(row["fingerprint"]).hex(),
                resolution=row["resolution"],
                resolved_by=row["resolved_by"],
                at=row["at"],
            )
            for row in result.mappings()
        ]

    async def delete(self, proposal_id: str, fingerprints: Sequence[str]) -> int:
        if not fingerprints:
            return 0
        result = await self._session.execute(
            delete(RESOLUTIONS).where(
                RESOLUTIONS.c.domain_id == self._domain,
                RESOLUTIONS.c.proposal_id == proposal_id,
                RESOLUTIONS.c.fingerprint.in_([bytes.fromhex(f) for f in fingerprints]),
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)
