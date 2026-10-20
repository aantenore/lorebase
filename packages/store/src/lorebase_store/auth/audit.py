"""Hash-chained audit log (design 10.4).

Each entry stores `entry_hash = sha256(prev_hash || canonical entry)` where `prev_hash` is the
hash of the entry before it in `seq` order (empty for the first) and the canonical entry is the
RFC 8785 serialization of its content. Changing or removing a stored row breaks the chain from
that point on, which `AuditRepository.verify` reports.

The chain spans the installation, so appends are serialized with a transaction-level advisory
lock and run in the system row-level security scope. Use a short transaction of their own: the
lock is held until commit.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.canonical import jcs_bytes
from lorebase_store.auth.scope import rls_scope
from lorebase_store.models import AuditEntry

AUDIT_LOCK_KEY = 0x4C42_4155_4449_5400
"""Advisory lock id serializing appends to the chain."""

VERIFY_BATCH = 1000


@dataclass(frozen=True)
class NewAuditEntry:
    """An action to record. `at` defaults to the current time."""

    actor: str
    action: str
    domain_id: str | None = None
    target: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    at: datetime | None = None


@dataclass(frozen=True)
class AuditRecord:
    """A stored audit entry."""

    seq: int
    domain_id: str | None
    at: datetime
    actor: str
    action: str
    target: str | None
    details: Mapping[str, Any]
    prev_hash: bytes | None
    entry_hash: bytes


@dataclass(frozen=True)
class ChainVerification:
    """Result of walking the chain. `bad_seq` is the first entry that does not verify."""

    checked: int
    bad_seq: int | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        """The whole chain verified."""
        return self.bad_seq is None


def _timestamp(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="microseconds")


def entry_digest(
    prev_hash: bytes | None,
    *,
    domain_id: str | None,
    at: datetime,
    actor: str,
    action: str,
    target: str | None,
    details: Mapping[str, Any],
) -> bytes:
    """`sha256(prev_hash || canonical entry)` for the given content."""
    canonical = jcs_bytes(
        {
            "domain_id": domain_id,
            "at": _timestamp(at),
            "actor": actor,
            "action": action,
            "target": target,
            "details": dict(details),
        }
    )
    return hashlib.sha256((prev_hash or b"") + canonical).digest()


def _record(row: AuditEntry) -> AuditRecord:
    return AuditRecord(
        seq=row.seq,
        domain_id=row.domain_id,
        at=row.at,
        actor=row.actor,
        action=row.action,
        target=row.target,
        details=row.details,
        prev_hash=row.prev_hash,
        entry_hash=row.entry_hash,
    )


def _digest_of(record: AuditRecord, prev_hash: bytes | None) -> bytes:
    return entry_digest(
        prev_hash,
        domain_id=record.domain_id,
        at=record.at,
        actor=record.actor,
        action=record.action,
        target=record.target,
        details=record.details,
    )


class AuditRepository:
    """Append, read and verify the audit log."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, entry: NewAuditEntry) -> AuditRecord:
        """Append an entry to the chain and return it."""
        at = entry.at or datetime.now(UTC)
        async with rls_scope(self._session, bypass=True):
            await self._session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": AUDIT_LOCK_KEY}
            )
            prev_hash = (
                await self._session.execute(
                    select(AuditEntry.entry_hash).order_by(AuditEntry.seq.desc()).limit(1)
                )
            ).scalar_one_or_none()
            digest = entry_digest(
                prev_hash,
                domain_id=entry.domain_id,
                at=at,
                actor=entry.actor,
                action=entry.action,
                target=entry.target,
                details=entry.details,
            )
            row = (
                await self._session.execute(
                    insert(AuditEntry)
                    .values(
                        domain_id=entry.domain_id,
                        at=at,
                        actor=entry.actor,
                        action=entry.action,
                        target=entry.target,
                        details=dict(entry.details),
                        prev_hash=prev_hash,
                        entry_hash=digest,
                    )
                    .returning(AuditEntry)
                )
            ).scalar_one()
            return _record(row)

    async def list(
        self, *, domain_id: str | None, limit: int, before_seq: int | None = None
    ) -> list[AuditRecord]:
        """Entries of a Domain (or the installation-wide ones when `domain_id` is None).

        Newest first; `before_seq` is the `seq` of the last entry of the previous page.
        """
        statement = select(AuditEntry).order_by(AuditEntry.seq.desc()).limit(limit)
        statement = statement.where(
            AuditEntry.domain_id.is_(None)
            if domain_id is None
            else AuditEntry.domain_id == domain_id
        )
        if before_seq is not None:
            statement = statement.where(AuditEntry.seq < before_seq)
        async with rls_scope(
            self._session,
            domain_ids=[] if domain_id is None else [domain_id],
            bypass=domain_id is None,
        ):
            return [_record(row) for row in (await self._session.execute(statement)).scalars()]

    async def verify(
        self, *, after_seq: int = 0, batch_size: int = VERIFY_BATCH
    ) -> ChainVerification:
        """Recompute the chain from `after_seq` (exclusive) and report the first broken entry.

        The entry at the start of the range is trusted as the anchor for its own `prev_hash`;
        verifying from 0 also requires the first entry to have none.
        """
        checked = 0
        cursor = after_seq
        previous: bytes | None = None
        async with rls_scope(self._session, bypass=True):
            while True:
                rows = (
                    await self._session.execute(
                        select(AuditEntry)
                        .where(AuditEntry.seq > cursor)
                        .order_by(AuditEntry.seq)
                        .limit(batch_size)
                    )
                ).scalars()
                batch = [_record(row) for row in rows]
                if not batch:
                    return ChainVerification(checked=checked)
                for record in batch:
                    anchor = checked == 0 and after_seq > 0
                    expected_prev = record.prev_hash if anchor else previous
                    if record.prev_hash != expected_prev:
                        return ChainVerification(checked, record.seq, "chain link broken")
                    if _digest_of(record, record.prev_hash) != record.entry_hash:
                        return ChainVerification(checked, record.seq, "entry content changed")
                    previous = record.entry_hash
                    cursor = record.seq
                    checked += 1
