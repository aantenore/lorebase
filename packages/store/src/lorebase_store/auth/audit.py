"""Hash-chained audit log (design 10.4).

The log holds one chain per Domain, plus one installation-wide chain for entries without a
Domain. Each entry stores `entry_hash = sha256(prev_hash || canonical entry)` where `prev_hash`
is the hash of the previous entry of the same chain in `seq` order (empty for the first) and the
canonical entry is the RFC 8785 serialization of its content. Changing or removing a stored row
breaks its chain from that point on, which `AuditRepository.verify` reports.

Appends to one chain are serialized with a transaction-level advisory lock keyed by the Domain
and run in the system row-level security scope; appends to different chains do not wait for
each other. The lock is held until commit: the Keeper writes in a short transaction of its own,
the commit pipeline inside the commit transaction, after the ref update.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.canonical import jcs_bytes
from lorebase_store.auth.scope import rls_scope
from lorebase_store.models import AuditEntry

_AUDIT_LOCK_SCOPE = b"lb1\x00audit-chain\x00"

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


def audit_lock_key(domain_id: str | None) -> int:
    """Advisory lock key of a Domain's chain (`None`: the installation-wide chain)."""
    digest = hashlib.sha256(_AUDIT_LOCK_SCOPE + (domain_id or "").encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _same_chain(domain_id: str | None) -> ColumnElement[bool]:
    return (
        AuditEntry.domain_id.is_(None) if domain_id is None else AuditEntry.domain_id == domain_id
    )


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
        """Append an entry to its Domain's chain and return it."""
        at = entry.at or datetime.now(UTC)
        async with rls_scope(self._session, bypass=True):
            await self._session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": audit_lock_key(entry.domain_id)},
            )
            prev_hash = (
                await self._session.execute(
                    select(AuditEntry.entry_hash)
                    .where(_same_chain(entry.domain_id))
                    .order_by(AuditEntry.seq.desc())
                    .limit(1)
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
        statement = (
            select(AuditEntry)
            .where(_same_chain(domain_id))
            .order_by(AuditEntry.seq.desc())
            .limit(limit)
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
        """Recompute every chain from `after_seq` (exclusive) and report the first broken entry.

        Entries are walked in `seq` order; each must link to the previous entry of its own
        chain. The first entry of each chain in the range is trusted as the anchor for its own
        `prev_hash`; verifying from 0 also requires the first entry of every chain to have none.
        """
        checked = 0
        cursor = after_seq
        heads: dict[str | None, bytes] = {}
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
                    if record.domain_id in heads:
                        expected_prev: bytes | None = heads[record.domain_id]
                    else:
                        expected_prev = record.prev_hash if after_seq > 0 else None
                    if record.prev_hash != expected_prev:
                        return ChainVerification(checked, record.seq, "chain link broken")
                    if _digest_of(record, record.prev_hash) != record.entry_hash:
                        return ChainVerification(checked, record.seq, "entry content changed")
                    heads[record.domain_id] = record.entry_hash
                    cursor = record.seq
                    checked += 1
