"""Run Alembic migrations programmatically and expose the expected head revisions.

The Alembic environment lives in `packages/store/migrations` (shipped in the wheel as
`lorebase_store/_migrations`). Settings not passed explicitly are loaded with `load_settings()`
by the environment; revisions read `config.attributes["store_settings"]`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from lorebase_store.settings import DatabaseSettings, StoreSettings

DATABASE_ATTRIBUTE = "database_settings"
STORE_ATTRIBUTE = "store_settings"
URL_ATTRIBUTE = "database_url"

DYNAMIC_INDEX_PREFIXES = ("embeddings_hnsw_", "mat_records_attr_")
"""Indexes created from configuration or schema at runtime; schema comparison skips them."""

PROFILE_INDEXES = ("ix_mat_records_values", "ix_mat_lexicon_branch_word")
"""Indexes of the models that a storage profile may leave out (`lorebase_store.storage`);
schema comparison skips them whether they exist or not."""

PARTITION_NAME = re.compile(r"^(?P<parent>[a-z_]+)_p(?P<remainder>\d+)$")
"""Hash partitions are named `<table>_p<remainder>`."""

_PACKAGED_SCRIPTS = Path(__file__).resolve().parent / "_migrations"
_SOURCE_SCRIPTS = Path(__file__).resolve().parents[2] / "migrations"


def script_location() -> Path:
    """Directory of the Alembic environment: the packaged copy, else the source tree."""
    return _PACKAGED_SCRIPTS if _PACKAGED_SCRIPTS.is_dir() else _SOURCE_SCRIPTS


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """Schema comparison filter: skip hash partitions, runtime-managed indexes and the
    indexes a storage profile manages."""
    if type_ == "index" and name in PROFILE_INDEXES:
        return False
    if not reflected or compare_to is not None or name is None:
        return True
    if type_ == "table":
        return PARTITION_NAME.match(name) is None
    if type_ == "index":
        return not name.startswith(DYNAMIC_INDEX_PREFIXES)
    return True


def alembic_config(
    database: DatabaseSettings | None = None,
    store: StoreSettings | None = None,
    *,
    url: str | None = None,
) -> Config:
    """Alembic configuration; `url` overrides `database.dsn`."""
    config = Config()
    config.set_main_option("script_location", str(script_location()))
    for key, value in (
        (DATABASE_ATTRIBUTE, database),
        (STORE_ATTRIBUTE, store),
        (URL_ATTRIBUTE, url),
    ):
        if value is not None:
            config.attributes[key] = value
    return config


def head_revisions() -> tuple[str, ...]:
    """Alembic head revisions of this code, for readiness checks."""
    return tuple(ScriptDirectory.from_config(alembic_config()).get_heads())


def upgrade(
    revision: str = "head",
    *,
    database: DatabaseSettings | None = None,
    store: StoreSettings | None = None,
    url: str | None = None,
) -> None:
    """Upgrade the database to `revision`."""
    command.upgrade(alembic_config(database, store, url=url), revision)


def downgrade(
    revision: str = "base",
    *,
    database: DatabaseSettings | None = None,
    store: StoreSettings | None = None,
    url: str | None = None,
) -> None:
    """Downgrade the database to `revision`."""
    command.downgrade(alembic_config(database, store, url=url), revision)
