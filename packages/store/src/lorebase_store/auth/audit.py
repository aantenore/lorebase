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

There are two streams, each in a table of its own with chains of its own (`AuditStream`):
actions (`audit_log`: mutations, denials, administrative events) and reads (`audit_read_log`,
written in batches by Keeper when `keeper.auth.audit.reads` is on). Reads are sampled and their
writer drops records under pressure, by design; in chains apart, neither their volume nor their
gaps reach the chains of actions, whose appends (inside commits too) never wait for reads, and
each stream is verified, anchored and kept for its own retention.

Removing the newest entries of a chain leaves a valid, shorter chain. `heads` gives the newest
entry of every chain, which `lorebase_store.auth.checkpoints` publishes outside the database so
that such a truncation can be detected.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any

from sqlalchemy import ColumnElement, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.canonical import jcs_bytes
from lorebase_store.auth.scope import rls_scope
from lorebase_store.models import AuditEntry, AuditReadEntry

_AUDIT_LOCK_SCOPE = b"lb1\x00audit-chain\x00"
_READ_LOCK_SCOPE = b"lb1\x00audit-read-chain\x00"

VERIFY_BATCH = 1000


class AuditStream(StrEnum):
    """The audit tables, each with chains of its own."""

    ACTIONS = "actions"
    """`audit_log`: mutations, denials and administrative events."""
    READS = "reads"
    """`audit_read_log`: reads recorded under `keeper.auth.audit.reads`."""


AuditModel = type[AuditEntry] | type[AuditReadEntry]

_MODELS: dict[AuditStream, AuditModel] = {
    AuditStream.ACTIONS: AuditEntry,
    AuditStream.READS: AuditReadEntry,
}
_LOCK_SCOPES = {AuditStream.ACTIONS: _AUDIT_LOCK_SCOPE, AuditStream.READS: _READ_LOCK_SCOPE}


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
class ChainHead:
    """The newest entry of one chain."""

    domain_id: str | None
    seq: int
    entry_hash: bytes


class Chains(Enum):
    """Chain selectors of `AuditRepository.verify` besides one Domain id."""

    ALL = "all"
    """Every chain: each Domain's and the installation-wide one."""


@dataclass(frozen=True)
class ChainVerification:
    """Result of walking the chain. `bad_seq` is the first entry that does not verify.

    `checked` counts the entries that verified before it, `chains` the chains they belong to,
    `last_seq` is the `seq` of the last entry that verified (a checkpoint for `after_seq`) and
    `bad_domain_id` the Domain of the broken entry (None for the installation-wide chain).
    """

    checked: int
    bad_seq: int | None = None
    reason: str | None = None
    chains: int = 0
    last_seq: int | None = None
    bad_domain_id: str | None = None

    @property
    def ok(self) -> bool:
        """The whole chain verified."""
        return self.bad_seq is None


def audit_lock_key(domain_id: str | None, stream: AuditStream = AuditStream.ACTIONS) -> int:
    """Advisory lock key of a Domain's chain (`None`: the installation-wide chain)."""
    digest = hashlib.sha256(_LOCK_SCOPES[stream] + (domain_id or "").encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _same_chain(model: AuditModel, domain_id: str | None) -> ColumnElement[bool]:
    return model.domain_id.is_(None) if domain_id is None else model.domain_id == domain_id


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


def _record(row: Any) -> AuditRecord:
    """The record of a row of either audit table."""
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


def _values(entry: NewAuditEntry, at: datetime, prev_hash: bytes | None) -> dict[str, Any]:
    """Column values of an entry linked to `prev_hash`."""
    return {
        "domain_id": entry.domain_id,
        "at": at,
        "actor": entry.actor,
        "action": entry.action,
        "target": entry.target,
        "details": dict(entry.details),
        "prev_hash": prev_hash,
        "entry_hash": entry_digest(
            prev_hash,
            domain_id=entry.domain_id,
            at=at,
            actor=entry.actor,
            action=entry.action,
            target=entry.target,
            details=entry.details,
        ),
    }


class AuditRepository:
    """Append, read and verify one stream of the audit log (default: actions)."""

    def __init__(self, session: AsyncSession, stream: AuditStream = AuditStream.ACTIONS) -> None:
        self._session = session
        self._stream = stream
        self._model: AuditModel = _MODELS[stream]

    async def _lock_and_head(self, domain_id: str | None) -> bytes | None:
        """Lock the chain until commit and read the hash of its newest entry."""
        model = self._model
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": audit_lock_key(domain_id, self._stream)},
        )
        head: bytes | None = (
            await self._session.execute(
                select(model.entry_hash)
                .where(_same_chain(model, domain_id))
                .order_by(model.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return head

    async def append(self, entry: NewAuditEntry) -> AuditRecord:
        """Append an entry to its Domain's chain and return it."""
        at = entry.at or datetime.now(UTC)
        async with rls_scope(self._session, bypass=True):
            prev_hash = await self._lock_and_head(entry.domain_id)
            row = (
                await self._session.execute(
                    insert(self._model)
                    .values(**_values(entry, at, prev_hash))
                    .returning(self._model)
                )
            ).scalar_one()
            return _record(row)

    async def append_many(self, entries: Sequence[NewAuditEntry]) -> int:
        """Append entries to their chains, keeping their order within each chain; returns
        the number appended. Chains are locked in a fixed order (by Domain id), so concurrent
        batches never deadlock, and each chain is read once per batch."""
        by_chain: dict[str | None, list[NewAuditEntry]] = {}
        for entry in entries:
            by_chain.setdefault(entry.domain_id, []).append(entry)
        now = datetime.now(UTC)
        async with rls_scope(self._session, bypass=True):
            for domain_id in sorted(by_chain, key=lambda d: (d is not None, d or "")):
                prev_hash = await self._lock_and_head(domain_id)
                rows = []
                for entry in by_chain[domain_id]:
                    row = _values(entry, entry.at or now, prev_hash)
                    prev_hash = row["entry_hash"]
                    rows.append(row)
                # Postgres assigns the identity in the order of the VALUES list.
                await self._session.execute(insert(self._model).values(rows))
        return len(entries)

    async def heads(self) -> list[ChainHead]:
        """The newest entry of every chain of the stream."""
        model = self._model
        statement = (
            select(model.domain_id, model.seq, model.entry_hash)
            .distinct(model.domain_id)
            .order_by(model.domain_id, model.seq.desc())
        )
        async with rls_scope(self._session, bypass=True):
            rows = (await self._session.execute(statement)).all()
        return [ChainHead(domain_id, int(seq), bytes(digest)) for domain_id, seq, digest in rows]

    async def entry_hashes(
        self, positions: Collection[tuple[str | None, int]]
    ) -> dict[tuple[str | None, int], bytes]:
        """`entry_hash` of the entries at `(domain_id, seq)`; absent ones are left out."""
        if not positions:
            return {}
        model = self._model
        seqs = sorted({seq for _, seq in positions})
        statement = select(model.domain_id, model.seq, model.entry_hash).where(model.seq.in_(seqs))
        async with rls_scope(self._session, bypass=True):
            rows = (await self._session.execute(statement)).all()
        wanted = set(positions)
        return {
            (domain_id, int(seq)): bytes(digest)
            for domain_id, seq, digest in rows
            if (domain_id, int(seq)) in wanted
        }

    async def list(
        self, *, domain_id: str | None, limit: int, before_seq: int | None = None
    ) -> list[AuditRecord]:
        """Entries of a Domain (or the installation-wide ones when `domain_id` is None).

        Newest first; `before_seq` is the `seq` of the last entry of the previous page.
        """
        model = self._model
        statement = (
            select(model)
            .where(_same_chain(model, domain_id))
            .order_by(model.seq.desc())
            .limit(limit)
        )
        if before_seq is not None:
            statement = statement.where(model.seq < before_seq)
        async with rls_scope(
            self._session,
            domain_ids=[] if domain_id is None else [domain_id],
            bypass=domain_id is None,
        ):
            return [_record(row) for row in (await self._session.execute(statement)).scalars()]

    async def verify(
        self,
        *,
        after_seq: int = 0,
        batch_size: int = VERIFY_BATCH,
        domain_id: str | Chains | None = Chains.ALL,
    ) -> ChainVerification:
        """Recompute chains from `after_seq` (exclusive) and report the first broken entry.

        `domain_id` selects one Domain's chain, None the installation-wide chain, and
        `Chains.ALL` (the default) every chain. Entries are read in `seq` order, `batch_size`
        at a time, so memory holds one batch and the head hash of each chain seen. Each entry
        must link to the previous entry of its own chain. The first entry of each chain in the
        range is trusted as the anchor for its own `prev_hash`; verifying from 0 also requires
        the first entry of every chain to have none.
        """
        checked = 0
        cursor = after_seq
        last_seq: int | None = None
        heads: dict[str | None, bytes] = {}
        model = self._model
        statement = select(model).order_by(model.seq).limit(batch_size)
        if not isinstance(domain_id, Chains):
            statement = statement.where(_same_chain(model, domain_id))

        def broken(record: AuditRecord, reason: str) -> ChainVerification:
            return ChainVerification(
                checked,
                record.seq,
                reason,
                chains=len(heads | {record.domain_id: b""}),
                last_seq=last_seq,
                bad_domain_id=record.domain_id,
            )

        async with rls_scope(self._session, bypass=True):
            while True:
                rows = (await self._session.execute(statement.where(model.seq > cursor))).scalars()
                batch = [_record(row) for row in rows]
                if not batch:
                    return ChainVerification(checked, chains=len(heads), last_seq=last_seq)
                for record in batch:
                    if record.domain_id in heads:
                        expected_prev: bytes | None = heads[record.domain_id]
                    else:
                        expected_prev = record.prev_hash if after_seq > 0 else None
                    if record.prev_hash != expected_prev:
                        return broken(record, "chain link broken")
                    if _digest_of(record, record.prev_hash) != record.entry_hash:
                        return broken(record, "entry content changed")
                    heads[record.domain_id] = record.entry_hash
                    cursor = last_seq = record.seq
                    checked += 1
