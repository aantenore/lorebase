"""Checks inside the security definer read functions (revision 0008), called as the
application role: bounded counts, a Domain inside the transaction scope, a fixed
search_path; and the upgrade refused while that role may create objects in their schemas."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Row, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_store import migrations
from lorebase_store.auth.scope import BYPASS_SETTING
from lorebase_store.commit import create_domain
from lorebase_store.db import create_engine
from lorebase_store.definer import READ_FUNCTIONS
from lorebase_store.repositories import scope_to_domain
from lorebase_store.repositories.refs import sqlstate
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings

from .read_support import seed_graph
from .support import AUTHOR, DOMAIN, PART, SUPPLIED_BY, SUPPLIER, VAT, Clock, domain

Sessions = async_sessionmaker[AsyncSession]

CALLS = {
    "lb_search": "SELECT * FROM lb_search(:domain, 'main', NULL, 'simple', 'bearing', "
    "CAST(:p_candidates AS integer), CAST(:p_limit AS integer))",
    "lb_links_in": "SELECT * FROM lb_links_in(:domain, 'main', :relation, 'S-1', "
    "CAST(:p_limit AS integer))",
    "lb_query_by_attribute": "SELECT * FROM lb_query_by_attribute(:domain, 'main', :kind, "
    ":attribute, 'text', 'eq', 'IT2', CAST(:p_limit AS integer))",
    "lb_neighborhood": "SELECT * FROM lb_neighborhood(:domain, 'main', CAST(:seeds AS jsonb), "
    "CAST(:p_depth AS integer), CAST('{}' AS jsonb), CAST(:p_fanout AS integer), "
    "CAST(:p_max_steps AS integer), CAST(:p_max_nodes AS integer))",
}
COUNTS = {
    "lb_search": ("p_candidates", "p_limit"),
    "lb_links_in": ("p_limit",),
    "lb_query_by_attribute": ("p_limit",),
    "lb_neighborhood": ("p_depth", "p_fanout", "p_max_steps", "p_max_nodes"),
}
ARGUMENTS: dict[str, Any] = {
    "domain": DOMAIN,
    "relation": SUPPLIED_BY,
    "kind": SUPPLIER,
    "attribute": VAT,
    "seeds": json.dumps([[PART, "P-1"]]),
    "p_limit": 5,
    "p_candidates": 50,
    "p_depth": 1,
    "p_fanout": 5,
    "p_max_steps": 50,
    "p_max_nodes": 10,
}
CEILINGS = StoreSettings().reads.definer_ceilings


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
def application_role(empty_database_url: str) -> Iterator[str]:
    """A role subject to row-level security that exists before the migration runs."""
    role = f"lorebase_app_{uuid.uuid4().hex[:8]}"
    asyncio.run(_execute(empty_database_url, f"CREATE ROLE {role} NOLOGIN"))
    try:
        yield role
    finally:
        asyncio.run(_execute(empty_database_url, f"DROP OWNED BY {role}", f"DROP ROLE {role}"))


@pytest.fixture
def database(application_role: str) -> DatabaseSettings:
    """Migrations grant the read functions to the application role."""
    return DatabaseSettings(application_role=application_role)


@pytest.fixture
async def sessions(
    engine: AsyncEngine, database_url: str, application_role: str, runtime: StoreRuntime
) -> Sessions:
    await _execute(
        database_url, f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {application_role}"
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await create_domain(session, domain(), author=AUTHOR, runtime=runtime, clock=Clock())
    async with factory() as session:
        await seed_graph(session, runtime)
    return factory


async def _as_role(session: AsyncSession, role: str) -> None:
    await session.execute(text(f"SET LOCAL ROLE {role}"))


async def _call(session: AsyncSession, function: str, **overrides: Any) -> list[Row[Any]]:
    return list(await session.execute(text(CALLS[function]), {**ARGUMENTS, **overrides}))


async def _refusal(session: AsyncSession, function: str, **overrides: Any) -> str | None:
    """SQLSTATE of the call, `None` when it succeeds (the transaction stays usable)."""
    try:
        async with session.begin_nested():
            await _call(session, function, **overrides)
    except DBAPIError as error:
        return sqlstate(error)
    return None


@pytest.mark.integration
async def test_counts_must_be_given_and_within_their_ceilings(
    sessions: Sessions, application_role: str
) -> None:
    async with sessions() as session, session.begin():
        await _as_role(session, application_role)
        await scope_to_domain(session, DOMAIN)
        for function, counts in COUNTS.items():
            assert await _call(session, function), function
            for name in counts:
                ceiling = getattr(CEILINGS, name.removeprefix("p_"))
                for value in (None, 0, -1, ceiling + 1):
                    refused = await _refusal(session, function, **{name: value})
                    assert refused == "22023", (function, name, value)
                assert await _refusal(session, function, **{name: ceiling}) is None


@pytest.mark.integration
async def test_the_domain_must_be_given_and_in_the_transaction_scope(
    sessions: Sessions, application_role: str
) -> None:
    async with sessions() as session, session.begin():
        await _as_role(session, application_role)
        for function in CALLS:
            assert await _refusal(session, function) == "42501", function
        await scope_to_domain(session, "rival")
        for function in CALLS:
            assert await _refusal(session, function) == "42501", function
            assert await _refusal(session, function, domain=None) == "22023", function
            assert await _refusal(session, function, domain="") == "22023", function
        await scope_to_domain(session, DOMAIN)
        for function in CALLS:
            assert await _call(session, function), function
    async with sessions() as session, session.begin():
        await _as_role(session, application_role)
        await session.execute(
            text("SELECT set_config(:name, 'on', true)"), {"name": BYPASS_SETTING}
        )
        for function in CALLS:
            assert await _call(session, function), function


@pytest.mark.integration
async def test_a_changed_search_path_is_refused(
    engine: AsyncEngine, sessions: Sessions, application_role: str
) -> None:
    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        found = await auto.execute(
            text(
                "SELECT CAST(CAST(oid AS regprocedure) AS text) FROM pg_proc "
                "WHERE proname = ANY(CAST(:names AS text[]))"
            ),
            {"names": list(READ_FUNCTIONS)},
        )
        signatures = list(found.scalars())
        assert len(signatures) == len(READ_FUNCTIONS)
        for change in ("RESET search_path", "SET search_path = public, pg_catalog, pg_temp"):
            for signature in signatures:
                await auto.exec_driver_sql(f"ALTER FUNCTION {signature} {change}")
            async with sessions() as session, session.begin():
                await _as_role(session, application_role)
                await scope_to_domain(session, DOMAIN)
                for function in CALLS:
                    assert await _refusal(session, function) == "55000", (change, function)


def _privileges(url: str, role: str) -> list[tuple[str, bool, bool, bool]]:
    """Per read function: its body has the checks, PUBLIC and `role` may execute it."""

    async def read() -> list[tuple[str, bool, bool, bool]]:
        engine = create_engine(DatabaseSettings(), url=url, pooled=False)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text(
                        "SELECT p.proname, p.prosrc LIKE '%transaction scope%', "
                        "EXISTS (SELECT 1 FROM aclexplode(p.proacl) a "
                        "        WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE'), "
                        "has_function_privilege(:role, p.oid, 'EXECUTE') "
                        "FROM pg_proc p WHERE p.proname = ANY(CAST(:names AS text[])) "
                        "ORDER BY 1"
                    ),
                    {"role": role, "names": list(READ_FUNCTIONS)},
                )
                return [tuple(row) for row in rows]  # type: ignore[misc]
        finally:
            await engine.dispose()

    return asyncio.run(read())


@pytest.mark.integration
def test_upgrade_and_downgrade_keep_the_grants(
    database_url: str, database: DatabaseSettings, store: StoreSettings, application_role: str
) -> None:
    names = sorted(READ_FUNCTIONS)
    checked = [(name, True, False, True) for name in names]
    assert _privileges(database_url, application_role) == checked
    migrations.downgrade("0007", database=database, store=store, url=database_url)
    assert _privileges(database_url, application_role) == [
        (name, False, False, True) for name in names
    ]
    migrations.upgrade(database=database, store=store, url=database_url)
    assert _privileges(database_url, application_role) == checked


@pytest.mark.integration
def test_upgrade_refuses_an_application_role_that_may_create_in_the_table_schema(
    empty_database_url: str, application_role: str, database: DatabaseSettings
) -> None:
    asyncio.run(
        _execute(empty_database_url, f"GRANT CREATE ON SCHEMA public TO {application_role}")
    )
    with pytest.raises(RuntimeError, match=f"{application_role}.*may create objects in schema"):
        migrations.upgrade(database=database, store=StoreSettings(), url=empty_database_url)
    asyncio.run(
        _execute(empty_database_url, f"REVOKE CREATE ON SCHEMA public FROM {application_role}")
    )
    migrations.upgrade(database=database, store=StoreSettings(), url=empty_database_url)
