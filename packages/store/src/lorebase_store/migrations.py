"""Run Alembic migrations programmatically and expose the expected head revisions.

The Alembic environment lives in `packages/store/migrations` (shipped in the wheel as
`lorebase_store/_migrations`). Settings not passed explicitly are loaded with `load_settings()`
by the environment; revisions read `config.attributes["store_settings"]`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import CommandError

from lorebase_store.settings import DatabaseSettings, StoreSettings

DATABASE_ATTRIBUTE = "database_settings"
STORE_ATTRIBUTE = "store_settings"
URL_ATTRIBUTE = "database_url"

DYNAMIC_INDEX_PREFIXES = ("embeddings_hnsw_", "mat_records_attr_", "ix_mat_records_bm25")
"""Indexes created from configuration or schema at runtime, the optional BM25 index
(`lorebase_store.bm25`) included; schema comparison skips them."""

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


class RevisionMismatchError(CommandError):
    """The database is at a revision this code does not know: a newer version migrated it."""


def unknown_revisions(script: ScriptDirectory, current: Iterable[str]) -> tuple[str, ...]:
    """Revisions of `current` (the database's) that the migration scripts of this code lack."""
    known = {revision.revision for revision in script.walk_revisions()}
    return tuple(sorted({revision for revision in current if revision not in known}))


def check_known_revisions(script: ScriptDirectory, current: Iterable[str]) -> None:
    """Refuse to migrate a database that a newer version already migrated.

    Alembic alone stops with "Can't locate revision"; this names both sides and the way out.
    """
    unknown = unknown_revisions(script, current)
    if not unknown:
        return
    heads = ", ".join(sorted(script.get_heads())) or "none"
    raise RevisionMismatchError(
        f"the database is at revision {', '.join(unknown)}, which this code does not know "
        f"(its newest revision is {heads}): a newer Lorebase migrated this database. Run the "
        "version that did (with docker compose: `docker compose build` then `up`, or "
        "`make up`, which rebuild the images from the checkout), or restore a backup taken "
        "before that upgrade. Older code cannot downgrade it."
    )


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
