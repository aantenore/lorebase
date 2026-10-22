"""Web session repository against Postgres (revision 0013)."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from lorebase_store.auth import SqlAuthStore
from lorebase_store.models.enums import PrincipalType

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


def key(name: str) -> bytes:
    return hashlib.sha256(name.encode()).digest()


async def user(auth_store: SqlAuthStore, subject: str = "sub-1") -> str:
    async with auth_store.unit() as unit:
        record = await unit.principals.add(PrincipalType.USER, "Ada", external_subject=subject)
    return record.id


async def add(
    auth_store: SqlAuthStore,
    principal_id: str,
    name: str,
    *,
    created: datetime = NOW,
    lifetime: timedelta = timedelta(hours=12),
    refresh_at: datetime | None = None,
) -> None:
    async with auth_store.unit() as unit:
        await unit.sessions.add(
            id_hash=key(name),
            principal_id=principal_id,
            subject="sub-1",
            external_groups=["analysts"],
            tokens=b"sealed",
            created_at=created,
            expires_at=created + lifetime,
            refresh_at=refresh_at,
        )


async def test_round_trip_touch_and_delete(auth_store: SqlAuthStore) -> None:
    principal = await user(auth_store)
    await add(auth_store, principal, "a")
    async with auth_store.unit() as unit:
        record = await unit.sessions.get(key("a"))
        assert record is not None
        assert record.principal_id == principal
        assert record.external_groups == ("analysts",)
        assert record.tokens == b"sealed"
        assert record.last_seen_at == NOW
        interval = timedelta(seconds=60)
        assert not await unit.sessions.touch(
            key("a"), NOW + timedelta(seconds=30), min_interval=interval
        )
        assert await unit.sessions.touch(
            key("a"), NOW + timedelta(seconds=90), min_interval=interval
        )
    async with auth_store.unit() as unit:
        record = await unit.sessions.get(key("a"))
        assert record is not None
        assert record.last_seen_at == NOW + timedelta(seconds=90)
        assert await unit.sessions.delete(key("a"))
        assert not await unit.sessions.delete(key("a"))
        assert await unit.sessions.get(key("a")) is None


async def test_only_digests_of_32_bytes_are_stored(auth_store: SqlAuthStore) -> None:
    principal = await user(auth_store)
    with pytest.raises(IntegrityError):
        async with auth_store.unit() as unit:
            await unit.sessions.add(
                id_hash=b"raw-session-id",
                principal_id=principal,
                subject="sub-1",
                external_groups=[],
                tokens=None,
                created_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                refresh_at=None,
            )


async def test_one_caller_claims_a_due_refresh(auth_store: SqlAuthStore) -> None:
    principal = await user(auth_store)
    await add(auth_store, principal, "a", refresh_at=NOW)
    lease = NOW + timedelta(seconds=30)

    async def claim() -> bool:
        async with auth_store.unit() as unit:
            return await unit.sessions.claim_refresh(key("a"), NOW, lease)

    results = await asyncio.gather(*(claim() for _ in range(5)))
    assert sorted(results) == [False, False, False, False, True]
    async with auth_store.unit() as unit:
        assert await unit.sessions.update_tokens(
            key("a"), tokens=b"new", external_groups=["ops"], refresh_at=NOW + timedelta(hours=1)
        )
        record = await unit.sessions.get(key("a"))
    assert record is not None
    assert (record.tokens, record.external_groups) == (b"new", ("ops",))


async def test_purge_removes_expired_and_idle_sessions_only(auth_store: SqlAuthStore) -> None:
    principal = await user(auth_store)
    await add(auth_store, principal, "live")
    await add(auth_store, principal, "expired", lifetime=timedelta(seconds=10))
    await add(auth_store, principal, "idle", created=NOW - timedelta(hours=2))
    async with auth_store.unit() as unit:
        purged = await unit.sessions.purge_expired(
            NOW + timedelta(seconds=20), NOW - timedelta(hours=1), limit=10
        )
        assert purged == 2
        assert await unit.sessions.get(key("live")) is not None


async def test_sessions_go_with_their_principal(auth_store: SqlAuthStore) -> None:
    principal = await user(auth_store)
    other = await user(auth_store, "sub-2")
    await add(auth_store, principal, "a")
    await add(auth_store, principal, "b")
    await add(auth_store, other, "c")
    async with auth_store.unit() as unit:
        assert await unit.sessions.delete_for_principal(principal) == 2
        assert await unit.sessions.get(key("c")) is not None
