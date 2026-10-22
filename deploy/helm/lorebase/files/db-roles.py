"""Application role of Keeper and Scribe, kept by the maintenance role before the migrations.

The Kubernetes counterpart of deploy/compose/postgres/app-role.sh (docs/deployment.md, section
"Database roles"). It connects with `database.dsn`, which the migration Job points at the
maintenance role (the owner of the tables), and keeps the role named by the application DSN:

- `CREATE_ROLE=true`: creates it when missing and sets `LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB
  NOCREATEROLE NOREPLICATION` and the password of the application DSN (the maintenance role needs
  `CREATEROLE`). Otherwise the role is created by someone else (the CloudNativePG operator from
  `managed.roles`, or a database administrator): the script waits up to `ROLE_WAIT_S` for it and
  refuses one with `SUPERUSER` or `BYPASSRLS`;
- revokes `CREATE` on `public` from `PUBLIC`, grants row rights on the tables and sequences of
  `public` (present ones, and later ones through default privileges of the maintenance role),
  `CONNECT, TEMPORARY` on the database and `EXECUTE` on the read functions an earlier migration
  created, and takes `UPDATE`, `DELETE`, `TRUNCATE`, `REFERENCES` and `TRIGGER` on `audit_log`
  back (revision 0015).

It is idempotent and refuses an application role equal to the maintenance role.

Environment: LOREBASE_CONFIG and the database settings of the migration, APP_DSN (the
application DSN), APP_ROLE (the expected role name), CREATE_ROLE, ROLE_WAIT_S.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import NoReturn

from sqlalchemy import make_url, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from lorebase_platform.settings import load_settings
from lorebase_store.db import create_engine
from lorebase_store.definer import grant_read_functions
from lorebase_store.settings import database_settings

ROLE = "CAST(:role AS text)"
SECRET = "CAST(:secret AS text)"
CREATE = (
    f"SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', {ROLE}, {SECRET}) "
    f"WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {ROLE})"
)
ALTER = (
    "SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE "
    f"NOREPLICATION PASSWORD %L', {ROLE}, {SECRET})"
)
GRANTS = (
    # Only the owner creates objects; the read functions rely on it (store schema notes).
    "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
    f"SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I', current_database(), {ROLE})",
    f"SELECT format('GRANT USAGE ON SCHEMA public TO %I', {ROLE})",
    "SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
    f"TO %I', {ROLE})",
    f"SELECT format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I', {ROLE})",
    # The audit log is append-only for the services (revision 0015).
    "SELECT format('REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE "
    f"public.audit_log FROM %I', {ROLE}) WHERE to_regclass('public.audit_log') IS NOT NULL",
    "SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT, INSERT, "
    f"UPDATE, DELETE ON TABLES TO %I', current_user, {ROLE})",
    "SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT USAGE, SELECT "
    f"ON SEQUENCES TO %I', current_user, {ROLE})",
)
_ROLE = text(f"SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = {ROLE}")


def fail(message: str) -> NoReturn:
    sys.exit(f"db-roles: {message}")


async def run(connection: AsyncConnection, statement: str, **params: str) -> None:
    """Run `statement`; a SELECT builds the statement to run (quoted with %I and %L)."""
    if not statement.startswith("SELECT"):
        await connection.exec_driver_sql(statement)
        return
    built = (await connection.execute(text(statement), params)).scalar_one_or_none()
    if built is not None:
        await connection.exec_driver_sql(built)


async def wait_for_role(connection: AsyncConnection, role: str, wait_s: float) -> None:
    deadline = time.monotonic() + wait_s
    while (await connection.execute(_ROLE, {"role": role})).first() is None:
        if time.monotonic() > deadline:
            fail(
                f"role {role!r} does not exist; create it (CloudNativePG: "
                "postgresql.cluster.roles) or set database.appRole.create=true"
            )
        await connection.rollback()
        await asyncio.sleep(2)


async def main() -> None:
    role = os.environ["APP_ROLE"]
    app = make_url(os.environ["APP_DSN"])
    create = os.environ.get("CREATE_ROLE", "false") == "true"
    wait_s = float(os.environ.get("ROLE_WAIT_S", "0"))
    database = database_settings(load_settings())
    owner = make_url(database.dsn.get_secret_value())
    if app.username != role:
        fail(f"the application DSN connects as {app.username!r}, database.appRole.name is {role!r}")
    if owner.username == role:
        fail(
            f"the application and maintenance DSNs use the same role {role!r}: the services would "
            "own the tables and skip the row-level security and audit log protections; use two "
            "roles (or database.allowSharedRole=true, development only)"
        )
    password = app.password if isinstance(app.password, str) else ""
    if create and not password:
        fail("the application DSN has no password")
    engine = create_engine(database, pooled=False, statement_timeout_ms=0)
    try:
        await setup(engine, role, create=create, password=password, wait_s=wait_s)
    except SQLAlchemyError as error:
        # The message of the driver only: the statement parameters hold the password.
        cause = error.orig if isinstance(error, DBAPIError) else error
        fail(f"{type(cause).__name__}: {cause}")
    finally:
        await engine.dispose()
    print(f"db-roles: role {role!r} is ready")


async def setup(
    engine: AsyncEngine, role: str, *, create: bool, password: str, wait_s: float
) -> None:
    """Create or wait for the role, then grant, as the maintenance role."""
    async with engine.connect() as connection:
        if create:
            await run(connection, CREATE, role=role, secret=password)
            await run(connection, ALTER, role=role, secret=password)
            await connection.commit()
        else:
            await wait_for_role(connection, role, wait_s)
        found = (await connection.execute(_ROLE, {"role": role})).one()
        if found.rolsuper or found.rolbypassrls:
            fail(f"role {role!r} has SUPERUSER or BYPASSRLS, which skip row-level security")
        for statement in GRANTS:
            await run(connection, statement, role=role)
        # A database migrated before the role existed: the read functions of the earlier
        # revisions, as the migration grants them for a new database.
        await grant_read_functions(connection, role)
        await connection.commit()


if __name__ == "__main__":
    asyncio.run(main())
