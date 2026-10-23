"""Migrations refuse a database that a newer version already migrated, naming both revisions."""

from __future__ import annotations

import asyncio

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from lorebase_store import migrations
from lorebase_store.settings import DatabaseSettings, StoreSettings

FUTURE = "9999_from_a_newer_version"


def script() -> ScriptDirectory:
    return ScriptDirectory.from_config(migrations.alembic_config())


def test_known_revisions_pass() -> None:
    heads = migrations.head_revisions()
    assert migrations.unknown_revisions(script(), heads) == ()
    migrations.check_known_revisions(script(), heads)
    migrations.check_known_revisions(script(), ())  # empty database


def test_an_unknown_revision_names_both_sides_and_the_fix() -> None:
    with pytest.raises(migrations.RevisionMismatchError) as caught:
        migrations.check_known_revisions(script(), [FUTURE])
    message = str(caught.value)
    assert FUTURE in message
    for head in migrations.head_revisions():
        assert head in message
    assert "docker compose build" in message


@pytest.mark.integration
def test_upgrade_refuses_a_database_ahead_of_the_code(
    database_url: str, database: DatabaseSettings, store: StoreSettings
) -> None:
    async def move_ahead() -> None:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE alembic_version SET version_num = :v"), {"v": FUTURE}
                )
        finally:
            await engine.dispose()

    asyncio.run(move_ahead())
    with pytest.raises(migrations.RevisionMismatchError, match=FUTURE):
        migrations.upgrade(database=database, store=store, url=database_url)
