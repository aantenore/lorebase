"""Alembic environment: async engine from the `database` section, layout from `store`.

`lorebase_store.migrations` may pass settings and a URL override through `config.attributes`;
anything missing (always, for `alembic upgrade head` on the command line) comes from
`load_settings()`, so `LOREBASE__DATABASE__DSN` and `LOREBASE_CONFIG` apply.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection

from lorebase_platform.settings import load_settings
from lorebase_store.db import async_url, create_engine
from lorebase_store.migrations import (
    DATABASE_ATTRIBUTE,
    STORE_ATTRIBUTE,
    URL_ATTRIBUTE,
    check_known_revisions,
    include_object,
)
from lorebase_store.models import metadata
from lorebase_store.settings import (
    DatabaseSettings,
    database_settings,
    store_settings,
)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

if DATABASE_ATTRIBUTE not in config.attributes or STORE_ATTRIBUTE not in config.attributes:
    loaded = load_settings()
    config.attributes.setdefault(DATABASE_ATTRIBUTE, database_settings(loaded))
    config.attributes.setdefault(STORE_ATTRIBUTE, store_settings(loaded))
database: DatabaseSettings = config.attributes[DATABASE_ATTRIBUTE]
url: str = config.attributes.get(URL_ATTRIBUTE) or database.dsn.get_secret_value()


def _configure(**options: Any) -> None:
    context.configure(
        target_metadata=metadata,
        include_object=include_object,
        compare_type=True,
        transaction_per_migration=True,
        **options,
    )


def run_offline() -> None:
    """Emit SQL instead of executing it."""
    _configure(
        url=async_url(url).render_as_string(hide_password=False),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _invoked_command() -> str | None:
    """Name of the `alembic` command line command being run; None when called from code."""
    invocation = getattr(config.cmd_opts, "cmd", None)
    return getattr(invocation[0], "__name__", None) if invocation else None


def _run_sync(connection: Connection) -> None:
    # `stamp --purge` is the manual way to repair the version table, so it is left alone.
    if _invoked_command() != "stamp":
        current = MigrationContext.configure(connection).get_current_heads()
        # The read began a transaction; end it so that migrations manage their own (some run
        # steps in autocommit blocks).
        if connection.in_transaction():
            connection.rollback()
        check_known_revisions(ScriptDirectory.from_config(config), current)
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    """Run migrations on a dedicated, unpooled connection."""
    engine = create_engine(
        database,
        url=url,
        pooled=False,
        statement_timeout_ms=database.migration_statement_timeout_ms,
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_sync)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())
