from __future__ import annotations

from pathlib import Path

import pytest

from lorebase_keeper.reading.settings import (
    ReadSettings,
    SearchSettings,
    read_settings,
    search_settings,
)
from lorebase_platform.settings import CONFIG_ENV_VAR, ConfigError, Settings, load_settings

EXAMPLE = Path(__file__).resolve().parents[4] / "config" / "lorebase.example.yaml"


def test_example_file_matches_code_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, str(EXAMPLE))
    loaded = load_settings()
    assert read_settings(loaded) == ReadSettings()
    assert search_settings(loaded) == SearchSettings()


def test_limits_reach_the_query_resolver() -> None:
    limits = read_settings(
        Settings(read={"records": {"max_limit": 70}, "query": {"max_via_depth": 2}})
    ).query_limits()
    assert limits.max_limit == 70
    assert limits.max_via_depth == 2


@pytest.mark.parametrize(
    ("section", "values", "needle"),
    [
        ("read", {"records": {"default_limit": 600}}, "read.records"),
        ("read", {"statement_timeout_ms": -1}, "read.statement_timeout_ms"),
        ("search", {"trigram_threshold": 2}, "search.trigram_threshold"),
    ],
)
def test_invalid_values_name_the_key(section: str, values: dict[str, object], needle: str) -> None:
    settings = Settings(**{section: values})
    loader = read_settings if section == "read" else search_settings
    with pytest.raises(ConfigError) as caught:
        loader(settings)
    assert needle in str(caught.value)
