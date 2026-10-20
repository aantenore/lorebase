"""Migrations against a real Postgres 16 with pgvector."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, insert, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from lorebase_store import migrations
from lorebase_store.db import create_engine
from lorebase_store.models import Domain, Job, Ref, Revision, metadata
from lorebase_store.models.enums import Materialization, RefKind
from lorebase_store.settings import DatabaseSettings, StoreSettings

pytestmark = pytest.mark.integration

LOCK_WAIT_TIMEOUT_S = 10.0
NOW = datetime(2026, 10, 20, tzinfo=UTC)


@asynccontextmanager
async def admin_engine(url: str) -> AsyncIterator[AsyncEngine]:
    """Unpooled engine that sees every Domain (RLS bypass)."""
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={"server_settings": {"lorebase.bypass_rls": "on"}},
    )
    try:
        yield engine
    finally:
        await engine.dispose()


async def scalars(connection: AsyncConnection, sql: str, **params: Any) -> list[Any]:
    return list((await connection.execute(text(sql), params)).scalars())


def h32(label: str) -> bytes:
    return label.encode().ljust(32, b".")


async def seed_domain(connection: AsyncConnection, domain: str) -> bytes:
    """A Domain with one Revision; returns the Revision id."""
    revision = h32(f"{domain}-rev-1")
    await connection.execute(
        insert(Domain).values(
            id=domain, display_name=domain, leaf_size=512, created_at=NOW, created_by="usr_t"
        )
    )
    await connection.execute(
        insert(Revision).values(
            domain_id=domain,
            id=revision,
            root=h32(f"{domain}-root"),
            generation=1,
            author="usr_t",
            message="init",
            origin={"type": "test"},
            stats={},
            created_at=NOW,
        )
    )
    return revision


async def public_tables(url: str) -> set[str]:
    async with admin_engine(url) as engine, engine.connect() as connection:
        rows = await scalars(
            connection,
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'",
        )
        types = await scalars(connection, "SELECT typname FROM pg_type WHERE typtype = 'e'")
    return set(rows) | {f"type:{name}" for name in types}


def test_upgrade_downgrade_round_trip(
    empty_database_url: str, database: DatabaseSettings, store: StoreSettings
) -> None:
    url = empty_database_url
    migrations.upgrade(database=database, store=store, url=url)
    assert set(metadata.tables) <= asyncio.run(public_tables(url))

    migrations.downgrade(database=database, store=store, url=url)
    assert asyncio.run(public_tables(url)) == set()

    migrations.upgrade(database=database, store=store, url=url)
    assert set(metadata.tables) <= asyncio.run(public_tables(url))


async def test_models_match_migrated_schema(database_url: str) -> None:
    def diff(connection: Connection) -> list[Any]:
        context = MigrationContext.configure(
            connection,
            opts={"include_object": migrations.include_object, "compare_type": True},
        )
        return list(compare_metadata(context, metadata))

    async with admin_engine(database_url) as engine, engine.connect() as connection:
        assert await connection.run_sync(diff) == []


async def test_post_create_ddl(database_url: str, store: StoreSettings) -> None:
    partitions = store.layout.hash_partitions
    async with admin_engine(database_url) as engine, engine.connect() as connection:
        compression = await scalars(
            connection,
            "SELECT DISTINCT a.attcompression::text FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "WHERE a.attname = 'body' AND c.relname ~ '^tree_nodes(_p[0-9]+)?$'",
        )
        assert compression == ["l"]
        for table in ("objects", "tree_nodes", "mat_records", "mat_links"):
            children = await scalars(
                connection,
                "SELECT count(*) FROM pg_inherits WHERE inhparent = CAST(:t AS regclass)",
                t=table,
            )
            assert children == [partitions], table
        assert await scalars(
            connection,
            "SELECT indexname FROM pg_indexes WHERE tablename = 'embeddings' "
            "AND indexname LIKE 'embeddings_hnsw_%'",
        ) == [f"embeddings_hnsw_{store.embeddings.dims}"]
        unprotected = await scalars(
            connection,
            "SELECT c.relname FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
            "AND a.attname = 'domain_id' AND NOT (c.relrowsecurity AND c.relforcerowsecurity)",
        )
        assert unprotected == []


async def test_engine_applies_statement_timeout(database_url: str) -> None:
    engine = create_engine(
        DatabaseSettings(statement_timeout_ms=1234), url=database_url, pooled=False
    )
    try:
        async with engine.connect() as connection:
            assert await scalars(connection, "SHOW statement_timeout") == ["1234ms"]
    finally:
        await engine.dispose()


async def test_row_level_security_scopes_reads_to_domains(database_url: str) -> None:
    role = f"lorebase_probe_{uuid.uuid4().hex[:8]}"
    async with admin_engine(database_url) as engine:
        async with engine.begin() as connection:
            for domain in ("a", "b"):
                await seed_domain(connection, domain)
                await connection.execute(
                    text(
                        "INSERT INTO objects (domain_id, hash, object_type, body, size) "
                        "VALUES (:d, :h, 1, '{}', 2)"
                    ),
                    {"d": domain, "h": h32(f"{domain}-obj")},
                )
            await connection.execute(text(f"CREATE ROLE {role} NOLOGIN"))
            await connection.execute(text(f"GRANT SELECT ON revisions, objects TO {role}"))

        async def visible(table: str, settings: dict[str, str]) -> list[str]:
            async with engine.begin() as connection:
                await connection.execute(text(f"SET LOCAL ROLE {role}"))
                await connection.execute(text("SELECT set_config('lorebase.bypass_rls', '', true)"))
                for name, value in settings.items():
                    await connection.execute(
                        text("SELECT set_config(:n, :v, true)"), {"n": name, "v": value}
                    )
                return await scalars(connection, f"SELECT domain_id FROM {table} ORDER BY 1")

        try:
            for table in ("revisions", "objects"):
                assert await visible(table, {}) == []
                assert await visible(table, {"lorebase.domain_ids": "a"}) == ["a"]
                assert await visible(table, {"lorebase.domain_ids": "a,b"}) == ["a", "b"]
                assert await visible(table, {"lorebase.bypass_rls": "on"}) == ["a", "b"]
        finally:
            async with engine.begin() as connection:
                await connection.execute(text(f"DROP OWNED BY {role}"))
                await connection.execute(text(f"DROP ROLE {role}"))


CAS = (
    "UPDATE refs SET head = :new, version = version + 1 "
    "WHERE domain_id = :domain AND name = :name AND head = :head AND version = :version"
)


async def wait_for_lock_waiter(engine: AsyncEngine) -> None:
    """Return once another session is blocked on a lock."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LOCK_WAIT_TIMEOUT_S
    async with engine.connect() as connection:
        while loop.time() < deadline:
            waiting = await scalars(
                connection,
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND datname = current_database()",
            )
            if waiting[0]:
                return
            await asyncio.sleep(0.05)
    raise AssertionError("no session waited on the ref row lock")


async def test_ref_compare_and_swap(database_url: str) -> None:
    async with admin_engine(database_url) as engine:
        async with engine.begin() as connection:
            first = await seed_domain(connection, "d")
            await connection.execute(
                insert(Ref).values(
                    domain_id="d",
                    name="main",
                    kind=RefKind.BRANCH,
                    head=first,
                    materialization=Materialization.FULL,
                    version=1,
                    created_at=NOW,
                    created_by="usr_t",
                )
            )
            rows = [
                {
                    "domain_id": "d",
                    "id": h32(f"d-rev-{n}"),
                    "root": h32("d-root"),
                    "generation": 2,
                    "author": "usr_t",
                    "message": f"rev {n}",
                    "origin": {},
                    "stats": {},
                    "created_at": NOW,
                }
                for n in (2, 3)
            ]
            await connection.execute(insert(Revision), rows)
        expected = {"domain": "d", "name": "main", "head": first, "version": 1}

        # Two writers race from the same observed head: exactly one moves the ref.
        async with engine.connect() as winner, engine.connect() as loser:
            await winner.begin()
            await loser.begin()
            moved = await winner.execute(text(CAS), {**expected, "new": h32("d-rev-2")})
            assert moved.rowcount == 1
            racing = asyncio.create_task(
                loser.execute(text(CAS), {**expected, "new": h32("d-rev-3")})
            )
            await wait_for_lock_waiter(engine)
            await winner.commit()
            assert (await racing).rowcount == 0
            await loser.rollback()

        async with engine.connect() as connection:
            head = await connection.execute(text("SELECT head, version FROM refs"))
            assert head.one() == (h32("d-rev-2"), 2)


CLAIM = (
    "SELECT id FROM jobs WHERE status = 'queued' AND run_after <= now() "
    "ORDER BY priority DESC, run_after LIMIT 1 FOR UPDATE SKIP LOCKED"
)


async def test_job_claim_skips_locked_rows(database_url: str) -> None:
    async with admin_engine(database_url) as engine:
        async with engine.begin() as connection:
            await connection.execute(
                insert(Job), [{"type": "merge", "priority": p} for p in (5, 1)]
            )
        async with engine.connect() as first, engine.connect() as second:
            await first.begin()
            await second.begin()
            claimed_first = await scalars(first, CLAIM)
            claimed_second = await scalars(second, CLAIM)
            async with engine.connect() as third:
                assert await scalars(third, CLAIM) == []
            for connection, claimed in ((first, claimed_first), (second, claimed_second)):
                await connection.execute(
                    text("UPDATE jobs SET status = 'running', locked_by = :w WHERE id = :id"),
                    {"w": str(id(connection)), "id": claimed[0]},
                )
                await connection.commit()

        assert claimed_first != claimed_second
        async with engine.connect() as connection:
            highest = await scalars(
                connection, "SELECT priority FROM jobs WHERE id = :id", id=claimed_first[0]
            )
            assert highest == [5]
            assert await scalars(connection, CLAIM) == []
