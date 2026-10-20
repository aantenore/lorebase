"""Audit entries and outbox events written by the commit pipeline (design 6.6 step 8, 10.4).

The audit log is a hash chain per Domain: `entry_hash = sha256(prev_hash || canonical entry)`,
where `prev_hash` is the hash of the Domain's previous entry. Appends to one chain are
serialized by a transaction-scoped advisory lock.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Table, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.canonical import canonical_bytes
from lorebase_core.revision import format_timestamp
from lorebase_store.models import metadata

AUDIT_LOG: Table = metadata.tables["audit_log"]
OUTBOX: Table = metadata.tables["outbox"]

_AUDIT_LOCK_SCOPE = b"lb1\x00audit-chain\x00"


def audit_lock_key(domain_id: str | None) -> int:
    """Advisory lock key of a Domain's audit chain (`None`: installation-wide chain)."""
    digest = hashlib.sha256(_AUDIT_LOCK_SCOPE + (domain_id or "").encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """Content of one audit entry, before chaining."""

    domain_id: str | None
    at: datetime
    actor: str
    action: str
    target: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def document(self) -> dict[str, Any]:
        """The JSON document hashed into the chain."""
        return {
            "domain_id": self.domain_id,
            "at": format_timestamp(self.at),
            "actor": self.actor,
            "action": self.action,
            "target": self.target,
            "details": self.details,
        }


def chain_hash(previous: bytes | None, entry: AuditEntry) -> bytes:
    """`sha256(prev_hash || canonical entry)`; the first entry has no previous hash."""
    return hashlib.sha256((previous or b"") + canonical_bytes(entry.document())).digest()


class AuditRepository:
    """Appends to the audit hash chain of a Domain."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, entry: AuditEntry) -> bytes:
        """Append `entry` to its Domain's chain; returns the entry hash."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": audit_lock_key(entry.domain_id)}
        )
        previous = await self._session.scalar(
            select(AUDIT_LOG.c.entry_hash)
            .where(
                AUDIT_LOG.c.domain_id == entry.domain_id
                if entry.domain_id is not None
                else AUDIT_LOG.c.domain_id.is_(None)
            )
            .order_by(AUDIT_LOG.c.seq.desc())
            .limit(1)
        )
        digest = chain_hash(previous, entry)
        await self._session.execute(
            insert(AUDIT_LOG).values(
                domain_id=entry.domain_id,
                at=entry.at,
                actor=entry.actor,
                action=entry.action,
                target=entry.target,
                details=entry.details,
                prev_hash=previous,
                entry_hash=digest,
            )
        )
        return digest


class OutboxRepository:
    """Events delivered after commit by a background worker."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(self, domain_id: str, event_type: str, payload: dict[str, Any]) -> int:
        result = await self._session.execute(
            insert(OUTBOX)
            .values(domain_id=domain_id, event_type=event_type, payload=payload)
            .returning(OUTBOX.c.id)
        )
        return int(result.scalar_one())
