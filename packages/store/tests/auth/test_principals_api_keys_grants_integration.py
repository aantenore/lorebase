"""Repositories for principals, groups, API keys and grants against Postgres."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_store.auth import (
    GrantRepository,
    PrefixTakenError,
    SqlAuthStore,
)
from lorebase_store.models import GroupMember
from lorebase_store.models.enums import GrantScope, PrincipalType

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
SeedDomain = Callable[[str], Awaitable[None]]


async def test_principal_round_trip_and_subject_lookup(auth_store: SqlAuthStore) -> None:
    async with auth_store.unit() as unit:
        user = await unit.principals.add(PrincipalType.USER, "Ada", external_subject="sub-1")
        service = await unit.principals.add(PrincipalType.SERVICE, "ingest bot")
    async with auth_store.unit() as unit:
        assert user.id.startswith("usr_")
        assert service.id.startswith("svc_")
        assert await unit.principals.get(user.id) == user
        assert await unit.principals.get_by_subject("sub-1") == user
        assert await unit.principals.get_by_subject("missing") is None
        assert await unit.principals.get("usr_missing") is None


async def test_external_subject_is_unique(auth_store: SqlAuthStore) -> None:
    async with auth_store.unit() as unit:
        await unit.principals.add(PrincipalType.USER, "Ada", external_subject="sub-1")
    with pytest.raises(IntegrityError):
        async with auth_store.unit() as unit:
            await unit.principals.add(PrincipalType.USER, "Other", external_subject="sub-1")


async def test_get_or_create_user_is_idempotent_under_concurrency(
    auth_store: SqlAuthStore,
) -> None:
    async def provision() -> tuple[str, bool]:
        async with auth_store.unit() as unit:
            principal, created = await unit.principals.get_or_create_user("sub-race", "Racer")
            return principal.id, created

    results = await asyncio.gather(*(provision() for _ in range(6)))
    assert len({principal_id for principal_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1


async def test_principals_list_pages_in_creation_order(auth_store: SqlAuthStore) -> None:
    added = []
    for n in range(5):  # one transaction each: created_at is the transaction start
        async with auth_store.unit() as unit:
            added.append(await unit.principals.add(PrincipalType.SERVICE, f"svc {n}"))
    async with auth_store.unit() as unit:
        await unit.principals.add(PrincipalType.USER, "someone", external_subject="s")
        first = await unit.principals.list(type_=PrincipalType.SERVICE, limit=3)
        last = first[-1]
        second = await unit.principals.list(
            type_=PrincipalType.SERVICE, limit=3, after=(last.created_at, last.id)
        )
    assert [p.id for p in first + second] == [p.id for p in added]


async def test_groups_resolve_by_membership_and_provider_name(
    auth_store: SqlAuthStore, sessions: async_sessionmaker[AsyncSession]
) -> None:
    async with auth_store.unit() as unit:
        member = await unit.principals.add(PrincipalType.USER, "Ada", external_subject="sub-1")
        stranger = await unit.principals.add(PrincipalType.USER, "Bob", external_subject="sub-2")
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO groups (id, display_name, external_group) VALUES "
                "('grp_a', 'A', NULL), ('grp_b', 'B', 'idp-b'), ('grp_c', 'C', 'idp-c')"
            )
        )
        session.add(GroupMember(group_id="grp_a", principal_id=member.id))
    async with auth_store.unit() as unit:
        assert await unit.groups.group_ids_for(member.id) == ["grp_a"]
        assert await unit.groups.group_ids_for(member.id, ["idp-b", "unknown"]) == [
            "grp_a",
            "grp_b",
        ]
        assert await unit.groups.group_ids_for(stranger.id) == []
        assert await unit.groups.subject_exists("grp_c")
        assert await unit.groups.subject_exists(member.id)
        assert not await unit.groups.subject_exists("nobody")


async def test_api_key_lookup_by_prefix_and_usability(auth_store: SqlAuthStore) -> None:
    async with auth_store.unit() as unit:
        service = await unit.principals.add(PrincipalType.SERVICE, "bot")
        key = await unit.api_keys.add(
            principal_id=service.id,
            prefix="abc123",
            key_hash=bytes(range(32)),
            scopes=["domain:acme"],
            expires_at=NOW + timedelta(days=1),
        )
    async with auth_store.unit() as unit:
        found = await unit.api_keys.get_by_prefix("abc123")
        assert found == key
        assert found is not None
        assert found.hash == bytes(range(32))
        assert found.scopes == ("domain:acme",)
        assert found.is_usable(NOW)
        assert not found.is_usable(NOW + timedelta(days=2))
        assert await unit.api_keys.get_by_prefix("nope") is None
        assert await unit.api_keys.get(key.id) == key


async def test_api_key_prefix_is_unique(auth_store: SqlAuthStore) -> None:
    async with auth_store.unit() as unit:
        service = await unit.principals.add(PrincipalType.SERVICE, "bot")
        await unit.api_keys.add(principal_id=service.id, prefix="same", key_hash=bytes(32))
        with pytest.raises(PrefixTakenError):
            await unit.api_keys.add(principal_id=service.id, prefix="same", key_hash=bytes(32))
        # the savepoint keeps the transaction usable
        await unit.api_keys.add(principal_id=service.id, prefix="other", key_hash=bytes(32))


async def test_api_key_hash_must_be_32_bytes(auth_store: SqlAuthStore) -> None:
    async with auth_store.unit() as unit:
        service = await unit.principals.add(PrincipalType.SERVICE, "bot")
        with pytest.raises(IntegrityError):
            await unit.api_keys.add(principal_id=service.id, prefix="short", key_hash=b"x")


async def test_touch_is_throttled_and_revoke_is_once(auth_store: SqlAuthStore) -> None:
    interval = timedelta(seconds=60)
    async with auth_store.unit() as unit:
        service = await unit.principals.add(PrincipalType.SERVICE, "bot")
        key = await unit.api_keys.add(principal_id=service.id, prefix="touch", key_hash=bytes(32))
        assert await unit.api_keys.touch(key.id, NOW, min_interval=interval)
        assert not await unit.api_keys.touch(
            key.id, NOW + timedelta(seconds=30), min_interval=interval
        )
        assert await unit.api_keys.touch(key.id, NOW + timedelta(seconds=61), min_interval=interval)
        assert await unit.api_keys.revoke(key.id, NOW)
        assert not await unit.api_keys.revoke(key.id, NOW)
        assert not await unit.api_keys.revoke("key_missing", NOW)
    async with auth_store.unit() as unit:
        stored = await unit.api_keys.get(key.id)
        assert stored is not None
        assert stored.last_used_at == NOW + timedelta(seconds=61)
        assert stored.revoked_at == NOW
        assert not stored.is_usable(NOW)


async def test_api_keys_list_by_domain_scope(auth_store: SqlAuthStore) -> None:
    async with auth_store.unit() as unit:
        service = await unit.principals.add(PrincipalType.SERVICE, "bot")
    mine = []
    for n in range(3):  # one transaction each: created_at is the transaction start
        async with auth_store.unit() as unit:
            mine.append(
                await unit.api_keys.add(
                    principal_id=service.id,
                    prefix=f"a{n}",
                    key_hash=bytes(32),
                    scopes=["domain:a"],
                )
            )
    async with auth_store.unit() as unit:
        await unit.api_keys.add(
            principal_id=service.id, prefix="b0", key_hash=bytes(32), scopes=["domain:b"]
        )
        await unit.api_keys.add(principal_id=service.id, prefix="u0", key_hash=bytes(32))
        page = await unit.api_keys.list_for_scope("domain:a", limit=2)
        rest = await unit.api_keys.list_for_scope(
            "domain:a", limit=2, after=(page[-1].created_at, page[-1].id)
        )
    assert [k.id for k in page + rest] == [k.id for k in mine]


async def test_grants_add_is_idempotent_and_validates_scope(
    auth_store: SqlAuthStore, seed_domain: SeedDomain
) -> None:
    await seed_domain("acme")
    async with auth_store.unit() as unit:
        first = await unit.grants.add(
            domain_id="acme", subject="usr_1", role="editor", created_by="usr_admin"
        )
        again = await unit.grants.add(
            domain_id="acme", subject="usr_1", role="editor", created_by="usr_other"
        )
        kind = await unit.grants.add(
            domain_id="acme",
            subject="usr_1",
            role="editor",
            created_by="usr_admin",
            scope_type=GrantScope.KIND,
            scope_ref="Supplier",
        )
        assert again == first
        assert kind.id != first.id
        assert (kind.scope_type, kind.scope_ref) == (GrantScope.KIND, "Supplier")
        with pytest.raises(ValueError, match="scope_ref"):
            await unit.grants.add(
                domain_id="acme",
                subject="usr_1",
                role="editor",
                created_by="usr_admin",
                scope_type=GrantScope.BRANCH,
            )
        with pytest.raises(ValueError, match="scope_ref"):
            await unit.grants.add(
                domain_id="acme",
                subject="usr_1",
                role="editor",
                created_by="usr_admin",
                scope_ref="main",
            )


async def test_grants_for_subjects_get_delete_and_list(
    auth_store: SqlAuthStore, seed_domain: SeedDomain
) -> None:
    await seed_domain("acme")
    await seed_domain("globex")
    async with auth_store.unit() as unit:
        a = await unit.grants.add(
            domain_id="acme", subject="usr_1", role="viewer", created_by="usr_admin"
        )
        b = await unit.grants.add(
            domain_id="acme", subject="grp_x", role="reviewer", created_by="usr_admin"
        )
        await unit.grants.add(
            domain_id="acme", subject="usr_2", role="admin", created_by="usr_admin"
        )
        c = await unit.grants.add(
            domain_id="globex", subject="usr_1", role="admin", created_by="usr_admin"
        )
    async with auth_store.unit() as unit:
        assert {g.id for g in await unit.grants.for_subjects("acme", ["usr_1", "grp_x"])} == {
            a.id,
            b.id,
        }
        assert await unit.grants.for_subjects("acme", []) == []
        assert await unit.grants.get("acme", a.id) == a
        assert await unit.grants.get("globex", a.id) is None
        anywhere = await unit.grants.for_subjects_anywhere(["usr_1"])
        assert {g.id for g in anywhere} == {a.id, c.id}
        listed = await unit.grants.list_domain("acme", limit=2)
        rest = await unit.grants.list_domain(
            "acme", limit=2, after=(listed[-1].created_at, listed[-1].id)
        )
        assert len(listed) == 2
        assert len(rest) == 1
        assert not await unit.grants.delete("globex", a.id)
        assert await unit.grants.delete("acme", a.id)
        assert await unit.grants.get("acme", a.id) is None


async def test_grant_for_unknown_domain_is_rejected(auth_store: SqlAuthStore) -> None:
    with pytest.raises(IntegrityError):
        async with auth_store.unit() as unit:
            await unit.grants.add(
                domain_id="ghost", subject="usr_1", role="viewer", created_by="usr_admin"
            )


@asynccontextmanager
async def restricted_role(engine: AsyncEngine) -> AsyncIterator[str]:
    """A login-less role without BYPASSRLS that may use the auth tables."""
    role = f"lorebase_auth_probe_{uuid.uuid4().hex[:8]}"
    async with engine.begin() as connection:
        await connection.execute(text(f"CREATE ROLE {role} NOLOGIN"))
        await connection.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON grants, audit_log TO {role}")
        )
        await connection.execute(text(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {role}"))
        await connection.execute(text(f"GRANT SELECT ON domains TO {role}"))
    try:
        yield role
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP OWNED BY {role}"))
            await connection.execute(text(f"DROP ROLE {role}"))


async def test_grant_lookups_respect_row_level_security(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    seed_domain: SeedDomain,
) -> None:
    await seed_domain("acme")
    await seed_domain("globex")
    async with sessions() as session, session.begin():
        repository = GrantRepository(session)
        await repository.add(
            domain_id="acme", subject="usr_1", role="viewer", created_by="usr_admin"
        )
        await repository.add(
            domain_id="globex", subject="usr_1", role="admin", created_by="usr_admin"
        )

    async with restricted_role(engine) as role, sessions() as session, session.begin():
        await session.execute(text(f"SET LOCAL ROLE {role}"))
        repository = GrantRepository(session)
        assert [g.domain_id for g in await repository.for_subjects("acme", ["usr_1"])] == ["acme"]
        assert [g.domain_id for g in await repository.for_subjects_anywhere(["usr_1"])] == [
            "acme",
            "globex",
        ]
        # the scope is restored: a bare query sees nothing again
        visible = (await session.execute(text("SELECT count(*) FROM grants"))).scalar_one()
        assert visible == 0
