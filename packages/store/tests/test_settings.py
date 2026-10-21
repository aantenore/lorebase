from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from lorebase_platform.settings import CONFIG_ENV_VAR, ConfigError, load_settings
from lorebase_store.db import async_url, create_engine
from lorebase_store.settings import (
    DEFAULT_DSN,
    DatabaseSettings,
    SchemaSettings,
    database_settings,
    schema_settings,
    store_settings,
)

EXAMPLE = Path(__file__).resolve().parents[3] / "config" / "lorebase.example.yaml"


def test_defaults() -> None:
    settings = load_settings()
    database = database_settings(settings)
    assert database.dsn.get_secret_value() == DEFAULT_DSN
    assert database.pool_size == 10
    store = store_settings(settings)
    assert store.layout.hash_partitions == 8
    assert store.embeddings.dims == 384


def test_example_file_matches_code_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = load_settings()
    monkeypatch.setenv(CONFIG_ENV_VAR, str(EXAMPLE))
    from_example = load_settings()
    assert database_settings(from_example) == database_settings(defaults)
    assert store_settings(from_example) == store_settings(defaults)
    assert schema_settings(from_example) == schema_settings(defaults)


def test_schema_section_feeds_validation_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    rules = schema_settings(load_settings()).rules()
    assert rules.allow_expressions is True
    monkeypatch.setenv("LOREBASE__SCHEMA__EXPRESSIONS_ENABLED", "false")
    monkeypatch.setenv("LOREBASE__SCHEMA__EXPRESSION_LIMITS__MAX_STEPS", "50")
    disabled = schema_settings(load_settings())
    assert disabled == SchemaSettings.model_validate(
        {"expressions_enabled": False, "expression_limits": {"max_steps": 50}}
    )
    assert disabled.rules().allow_expressions is False
    assert disabled.rules().expression_limits is not None
    assert disabled.rules().expression_limits.max_steps == 50


def test_compose_style_dsn_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    dsn = "postgresql+asyncpg://app:hunter2-long@db:5432/lorebase"
    monkeypatch.setenv("LOREBASE_DB_DSN", dsn)
    monkeypatch.setenv("LOREBASE__DATABASE__DSN", "secret://env/LOREBASE_DB_DSN")
    database = database_settings(load_settings())
    assert isinstance(database.dsn, SecretStr)
    assert database.dsn.get_secret_value() == dsn
    assert "hunter2" not in repr(database)


def test_alias_sets_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOREBASE_DB_DSN", "postgresql://alias/db")
    assert database_settings(load_settings()).dsn.get_secret_value() == "postgresql://alias/db"


def test_explicit_env_key_beats_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOREBASE_DB_DSN", "postgresql://alias/db")
    monkeypatch.setenv("LOREBASE__DATABASE__DSN", "postgresql://explicit/db")
    assert database_settings(load_settings()).dsn.get_secret_value() == "postgresql://explicit/db"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("LOREBASE__STORE__EMBEDDINGS__DIMS", "4096"),
        ("LOREBASE__STORE__LAYOUT__HASH_PARTITIONS", "0"),
        ("LOREBASE__DATABASE__POOL_SIZE", "0"),
    ],
)
def test_invalid_values_fail_at_startup(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv(key, value)
    dotted = key.removeprefix("LOREBASE__").lower().replace("__", r"\.")
    with pytest.raises(ConfigError, match=dotted):
        load_settings()


@pytest.mark.parametrize(
    "url",
    ["postgres://u@h/db", "postgresql://u@h/db", "postgresql+asyncpg://u@h/db"],
)
def test_async_url_selects_asyncpg(url: str) -> None:
    assert async_url(url).drivername == "postgresql+asyncpg"


def test_async_url_rejects_other_databases() -> None:
    with pytest.raises(ValueError, match="unsupported database driver"):
        async_url("mysql://u@h/db")


def test_engine_uses_pool_settings() -> None:
    engine = create_engine(DatabaseSettings(pool_size=3, max_overflow=1))
    try:
        pool = engine.sync_engine.pool
        assert pool.size() == 3  # type: ignore[attr-defined]
        assert engine.url.drivername == "postgresql+asyncpg"
    finally:
        engine.sync_engine.dispose()


# Read replicas ------------------------------------------------------------------------------


def test_read_replicas_are_off_by_default() -> None:
    database = database_settings(load_settings())
    assert database.read_dsn == ()
    assert database.read_max_lag_ms == 5000
    assert database.read_fallback_to_primary is True


@pytest.mark.parametrize(
    "value",
    [
        "postgresql://r1/db,postgresql://r2/db",
        " postgresql://r1/db , postgresql://r2/db ,",
        ["postgresql://r1/db", "postgresql://r2/db"],
    ],
)
def test_read_dsn_accepts_a_string_or_a_list(value: str | list[str]) -> None:
    database = DatabaseSettings(read_dsn=value)  # type: ignore[arg-type]
    assert [dsn.get_secret_value() for dsn in database.read_dsn] == [
        "postgresql://r1/db",
        "postgresql://r2/db",
    ]


def test_read_dsn_entries_are_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPLICA_ONE", "postgresql://app:hunter2-long@r1/db")
    monkeypatch.setenv("LOREBASE__DATABASE__READ_DSN", '["secret://env/REPLICA_ONE"]')
    database = database_settings(load_settings())
    assert database.read_dsn[0].get_secret_value() == "postgresql://app:hunter2-long@r1/db"
    assert "hunter2" not in repr(database)


def test_alias_sets_read_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOREBASE_DB_READ_DSN", "postgresql://r1/db,postgresql://r2/db")
    assert len(database_settings(load_settings()).read_dsn) == 2


def test_unresolvable_read_dsn_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOREBASE__DATABASE__READ_DSN", "secret://env/LOREBASE_NO_SUCH_VARIABLE")
    with pytest.raises(ConfigError, match=r"database\.read_dsn"):
        load_settings()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("LOREBASE__DATABASE__READ_MAX_LAG_MS", "-1"),
        ("LOREBASE__DATABASE__READ_PROBE_TIMEOUT_MS", "0"),
    ],
)
def test_invalid_read_values_fail_at_startup(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError, match=key.removeprefix("LOREBASE__").lower().split("__")[1]):
        load_settings()
