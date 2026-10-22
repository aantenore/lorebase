"""The read stream of the audit log, chain heads and signed checkpoints against Postgres."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_store.auth import AuditRepository, AuditStream, NewAuditEntry
from lorebase_store.auth.checkpoints import (
    Checkpoint,
    CheckpointError,
    compare,
    parse_document,
    signed_document,
)

pytestmark = pytest.mark.integration

AT = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)


def read(n: int, domain_id: str | None) -> NewAuditEntry:
    return NewAuditEntry(
        actor="usr_1", action="read", domain_id=domain_id, details={"n": n, "status": 200}
    )


async def test_batches_of_reads_form_chains_of_their_own(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session, session.begin():
        await AuditRepository(session).append(NewAuditEntry(actor="usr_1", action="x.create"))
        reads = AuditRepository(session, AuditStream.READS)
        assert await reads.append_many([read(n, ("acme", "globex", None)[n % 3]) for n in range(7)])
        assert await reads.append_many([read(n, "acme") for n in range(7, 10)])
    async with sessions() as session, session.begin():
        reads = AuditRepository(session, AuditStream.READS)
        verification = await reads.verify(batch_size=4)
        assert verification.ok
        assert (verification.checked, verification.chains) == (10, 3)
        acme = list(reversed(await reads.list(domain_id="acme", limit=20)))
        assert [e.details["n"] for e in acme] == [0, 3, 6, 7, 8, 9]
        actions = await AuditRepository(session).verify()
        assert (actions.ok, actions.checked) == (True, 1)
        heads = {h.domain_id: h for h in await reads.heads()}
        assert set(heads) == {"acme", "globex", None}
        assert heads["acme"].entry_hash == acme[-1].entry_hash


async def test_checkpoints_reveal_truncation_and_rewrites(
    sessions: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    key = Ed25519PrivateKey.generate()
    async with sessions() as session, session.begin():
        log = AuditRepository(session)
        for n in range(4):
            await log.append(NewAuditEntry(actor="usr_1", action=f"a{n}", domain_id="acme"))
        await log.append(NewAuditEntry(actor="usr_1", action="b", domain_id="globex"))
        heads = await log.heads()
    document = signed_document(Checkpoint(AuditStream.ACTIONS, AT, "main", tuple(heads)), key)
    checkpoint = parse_document(document, key.public_key())
    assert checkpoint.chains == tuple(heads)

    async with sessions() as session, session.begin():
        assert await compare(AuditRepository(session), [checkpoint]) == []

    acme_head = next(h for h in heads if h.domain_id == "acme")
    async with engine.begin() as connection:  # the table owner cuts the tail of a chain
        await connection.execute(
            text("DELETE FROM audit_log WHERE seq = :seq"), {"seq": acme_head.seq}
        )
    async with sessions() as session, session.begin():
        log = AuditRepository(session)
        assert (await log.verify()).ok  # the shorter chain still verifies
        [finding] = await compare(log, [checkpoint])
        assert (finding.domain_id, finding.seq, finding.problem) == (
            "acme",
            acme_head.seq,
            "missing",
        )
        assert await compare(log, [checkpoint], domain_ids=["globex"]) == []

    globex_head = next(h for h in heads if h.domain_id == "globex")
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE audit_log SET entry_hash = sha256(entry_hash) WHERE seq = :seq"),
            {"seq": globex_head.seq},
        )
    async with sessions() as session, session.begin():
        problems = {
            f.domain_id: f.problem for f in await compare(AuditRepository(session), [checkpoint])
        }
    assert problems == {"acme": "missing", "globex": "changed"}


def test_documents_need_the_signers_key() -> None:
    key = Ed25519PrivateKey.generate()
    document = signed_document(Checkpoint(AuditStream.READS, AT, "main", ()), key)
    assert parse_document(document, key.public_key()).stream is AuditStream.READS
    with pytest.raises(CheckpointError, match="signature"):
        parse_document(document, Ed25519PrivateKey.generate().public_key())
    with pytest.raises(CheckpointError, match="signature"):
        parse_document(document.replace(b'"reads"', b'"actions"'), key.public_key())
    with pytest.raises(CheckpointError):
        parse_document(b"[]", key.public_key())
