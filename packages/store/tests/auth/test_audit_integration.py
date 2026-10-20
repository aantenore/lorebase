"""Hash-chained audit log against Postgres."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_store.auth import AuditRepository, NewAuditEntry, SqlAuthStore, entry_digest

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
SeedDomain = Callable[[str], Awaitable[None]]


async def append(auth_store: SqlAuthStore, n: int, domain_id: str | None = None) -> int:
    async with auth_store.unit() as unit:
        record = await unit.audit.append(
            NewAuditEntry(
                actor="usr_1",
                action=f"test.action.{n}",
                domain_id=domain_id,
                target=f"t{n}",
                details={"n": n, "outcome": "success"},
            )
        )
    return record.seq


async def test_entries_are_chained(auth_store: SqlAuthStore) -> None:
    for n in range(3):
        await append(auth_store, n)
    async with auth_store.unit() as unit:
        entries = list(reversed(await unit.audit.list(domain_id=None, limit=10)))
        assert entries[0].prev_hash is None
        assert entries[1].prev_hash == entries[0].entry_hash
        assert entries[2].prev_hash == entries[1].entry_hash
        first = entries[0]
        assert first.entry_hash == entry_digest(
            None,
            domain_id=first.domain_id,
            at=first.at,
            actor=first.actor,
            action=first.action,
            target=first.target,
            details=first.details,
        )
        assert len(first.entry_hash) == hashlib.sha256().digest_size
        verification = await unit.audit.verify()
    assert verification.ok
    assert verification.checked == 3


async def test_empty_log_verifies(auth_store: SqlAuthStore) -> None:
    async with auth_store.unit() as unit:
        assert (await unit.audit.verify()).ok


async def test_concurrent_appends_keep_one_chain(auth_store: SqlAuthStore) -> None:
    await asyncio.gather(*(append(auth_store, n) for n in range(12)))
    async with auth_store.unit() as unit:
        verification = await unit.audit.verify(batch_size=5)
    assert verification.ok
    assert verification.checked == 12


async def test_modified_row_is_detected(auth_store: SqlAuthStore, engine: AsyncEngine) -> None:
    seqs = [await append(auth_store, n) for n in range(4)]
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE audit_log SET actor = 'usr_evil' WHERE seq = :seq"), {"seq": seqs[2]}
        )
    async with auth_store.unit() as unit:
        verification = await unit.audit.verify()
    assert not verification.ok
    assert verification.bad_seq == seqs[2]
    assert verification.reason == "entry content changed"
    assert verification.checked == 2


async def test_modified_details_are_detected(auth_store: SqlAuthStore, engine: AsyncEngine) -> None:
    seqs = [await append(auth_store, n) for n in range(2)]
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE audit_log SET details = '{\"n\": 99}'::jsonb WHERE seq = :seq"),
            {"seq": seqs[0]},
        )
    async with auth_store.unit() as unit:
        assert (await unit.audit.verify()).bad_seq == seqs[0]


async def test_deleted_row_breaks_the_chain(auth_store: SqlAuthStore, engine: AsyncEngine) -> None:
    seqs = [await append(auth_store, n) for n in range(4)]
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM audit_log WHERE seq = :seq"), {"seq": seqs[1]})
    async with auth_store.unit() as unit:
        verification = await unit.audit.verify()
    assert verification.bad_seq == seqs[2]
    assert verification.reason == "chain link broken"


async def test_verify_can_start_after_a_checkpoint(
    auth_store: SqlAuthStore, engine: AsyncEngine
) -> None:
    seqs = [await append(auth_store, n) for n in range(4)]
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE audit_log SET actor = 'usr_evil' WHERE seq = :seq"), {"seq": seqs[0]}
        )
    async with auth_store.unit() as unit:
        assert (await unit.audit.verify(after_seq=seqs[1])).ok
        assert (await unit.audit.verify()).bad_seq == seqs[0]


async def test_list_filters_by_domain_and_pages_newest_first(
    auth_store: SqlAuthStore, seed_domain: SeedDomain
) -> None:
    await seed_domain("acme")
    acme = [await append(auth_store, n, "acme") for n in range(3)]
    await append(auth_store, 9, None)
    async with auth_store.unit() as unit:
        page = await unit.audit.list(domain_id="acme", limit=2)
        rest = await unit.audit.list(domain_id="acme", limit=2, before_seq=page[-1].seq)
        installation = await unit.audit.list(domain_id=None, limit=10)
    assert [e.seq for e in page + rest] == sorted(acme, reverse=True)
    assert [e.action for e in installation] == ["test.action.9"]


async def test_entries_survive_domain_deletion(
    auth_store: SqlAuthStore, engine: AsyncEngine, seed_domain: SeedDomain
) -> None:
    await seed_domain("acme")
    await append(auth_store, 1, "acme")
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM domains WHERE id = 'acme'"))
    async with auth_store.unit() as unit:
        assert len(await unit.audit.list(domain_id="acme", limit=5)) == 1
        assert (await unit.audit.verify()).ok


@asynccontextmanager
async def restricted_role(engine: AsyncEngine) -> AsyncIterator[str]:
    role = f"lorebase_audit_probe_{uuid.uuid4().hex[:8]}"
    async with engine.begin() as connection:
        await connection.execute(text(f"CREATE ROLE {role} NOLOGIN"))
        await connection.execute(text(f"GRANT SELECT, INSERT ON audit_log TO {role}"))
        await connection.execute(text(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {role}"))
    try:
        yield role
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP OWNED BY {role}"))
            await connection.execute(text(f"DROP ROLE {role}"))


async def test_append_and_verify_work_for_a_role_under_row_level_security(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    seed_domain: SeedDomain,
) -> None:
    await seed_domain("acme")
    async with restricted_role(engine) as role:
        for n, domain in enumerate(("acme", None)):
            async with sessions() as session, session.begin():
                await session.execute(text(f"SET LOCAL ROLE {role}"))
                await AuditRepository(session).append(
                    NewAuditEntry(actor="usr_1", action=f"a{n}", domain_id=domain)
                )
        async with sessions() as session, session.begin():
            await session.execute(text(f"SET LOCAL ROLE {role}"))
            repository = AuditRepository(session)
            assert (await repository.verify()).checked == 2
            assert len(await repository.list(domain_id="acme", limit=5)) == 1
            visible = (await session.execute(text("SELECT count(*) FROM audit_log"))).scalar_one()
            assert visible == 0
