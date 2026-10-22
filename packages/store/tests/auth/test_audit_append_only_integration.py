"""Revision 0015: the application role may read and append audit entries, never change them."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_store import migrations
from lorebase_store.auth import AuditRepository, NewAuditEntry
from lorebase_store.db import create_engine
from lorebase_store.repositories.refs import sqlstate
from lorebase_store.settings import DatabaseSettings, StoreSettings

pytestmark = pytest.mark.integration

INSUFFICIENT_PRIVILEGE = "42501"
CHANGES = (
    "UPDATE audit_log SET actor = 'forged'",
    "DELETE FROM audit_log",
    "TRUNCATE audit_log",
)


async def _execute(url: str, *statements: str) -> None:
    engine = create_engine(DatabaseSettings(), url=url, pooled=False)
    try:
        async with engine.connect() as connection:
            auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
            for statement in statements:
                await auto.exec_driver_sql(statement)
    finally:
        await engine.dispose()


@pytest.fixture
def roles(empty_database_url: str) -> Iterator[tuple[str, str]]:
    """The application role and another role, both granted every table right the way the
    compose `db-roles` script grants them (default privileges of the owner) before migrating."""
    suffix = uuid.uuid4().hex[:8]
    app, other = f"lorebase_app_{suffix}", f"lorebase_other_{suffix}"
    asyncio.run(
        _execute(
            empty_database_url,
            f"CREATE ROLE {app} NOLOGIN",
            f"CREATE ROLE {other} NOLOGIN",
            f"GRANT USAGE ON SCHEMA public TO {app}, {other}",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app}",
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {other}",
        )
    )
    try:
        yield app, other
    finally:
        asyncio.run(
            _execute(
                empty_database_url,
                *(f"DROP OWNED BY {role}" for role in (app, other)),
                f"DROP ROLE {app}",
                f"DROP ROLE {other}",
            )
        )


@pytest.fixture
def database(roles: tuple[str, str]) -> DatabaseSettings:
    return DatabaseSettings(application_role=roles[0])


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _refusal(session: AsyncSession, statement: str) -> str | None:
    try:
        async with session.begin_nested():
            await session.execute(text(statement))
    except DBAPIError as error:
        return sqlstate(error)
    return None


async def test_the_application_role_appends_but_cannot_change_entries(
    engine: AsyncEngine, roles: tuple[str, str]
) -> None:
    app, other = roles
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(text(f"SET LOCAL ROLE {app}"))
        record = await AuditRepository(session).append(
            NewAuditEntry(actor="usr_1", action="test.append", details={"outcome": "success"})
        )
        assert record.seq > 0
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        for statement in CHANGES:
            assert await _refusal(session, statement) == INSUFFICIENT_PRIVILEGE, statement
    async with sessions() as session, session.begin():
        await session.execute(text(f"SET LOCAL ROLE {other}"))
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        for statement in CHANGES:
            assert await _refusal(session, statement) == INSUFFICIENT_PRIVILEGE, statement


async def test_other_tables_keep_their_rights(engine: AsyncEngine, roles: tuple[str, str]) -> None:
    app, _ = roles
    async with engine.connect() as connection:
        rights = (
            await connection.execute(
                text(
                    "SELECT has_table_privilege(:r, 'audit_log', 'SELECT'), "
                    "has_table_privilege(:r, 'audit_log', 'INSERT'), "
                    "has_table_privilege(:r, 'audit_log', 'UPDATE'), "
                    "has_table_privilege(:r, 'audit_log', 'DELETE'), "
                    "has_table_privilege(:r, 'grants', 'UPDATE'), "
                    "has_table_privilege(:r, 'grants', 'DELETE')"
                ),
                {"r": app},
            )
        ).one()
    assert tuple(rights) == (True, True, False, False, True, True)


def test_downgrade_gives_the_rights_back(
    database_url: str, database: DatabaseSettings, roles: tuple[str, str]
) -> None:
    migrations.downgrade("0014", database=database, store=StoreSettings(), url=database_url)

    async def rights() -> tuple[bool, bool]:
        engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
        try:
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT has_table_privilege(:r, 'audit_log', 'UPDATE'), "
                            "has_table_privilege(:r, 'audit_log', 'DELETE')"
                        ),
                        {"r": roles[0]},
                    )
                ).one()
                return bool(row[0]), bool(row[1])
        finally:
            await engine.dispose()

    assert asyncio.run(rights()) == (True, True)
