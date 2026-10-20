"""Settings come from the command line, then the environment, then the config file."""

from __future__ import annotations

from pathlib import Path

import pytest

from lorebase_cli.failures import CliError
from lorebase_cli.settings import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_LIST_LIMIT,
    default_config_path,
    load_settings,
)

CONFIG = """
profile = "local"
domain = "base-domain"

[profiles.local]
url = "http://local.test"
api_key = "lbk_local"
branch = "work"

[profiles.prod]
url = "https://prod.test"
token = "tok_prod"
domain = "prod-domain"
timeout = 5

[import]
batch_size = 250

[output]
list_limit = 7
max_cell_width = 30
"""


def write(tmp_path: Path, text: str = CONFIG, mode: int = 0o600) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    path.chmod(mode)
    return path


def test_defaults_without_any_configuration() -> None:
    settings = load_settings(env={"XDG_CONFIG_HOME": "/nonexistent"})
    assert settings.url is None
    assert settings.batch_size == DEFAULT_BATCH_SIZE
    assert settings.list_limit == DEFAULT_LIST_LIMIT
    assert settings.config_path is None


def test_default_path_follows_xdg_then_home(isolated_configuration: Path) -> None:
    assert default_config_path({"XDG_CONFIG_HOME": "/x"}) == Path("/x/lorebase/config.toml")
    assert default_config_path({}) == isolated_configuration / ".config/lorebase/config.toml"


def test_file_gives_the_default_profile_and_sections(tmp_path: Path) -> None:
    settings = load_settings(env={}, config_path=write(tmp_path))
    assert settings.profile == "local"
    assert settings.url == "http://local.test"
    assert settings.api_key == "lbk_local"
    assert settings.domain == "base-domain"  # top-level keys apply to every profile
    assert settings.branch == "work"
    assert settings.batch_size == 250
    assert settings.list_limit == 7
    assert settings.max_cell_width == 30


def test_profile_flag_and_environment_pick_another_profile(tmp_path: Path) -> None:
    path = write(tmp_path)
    flag = load_settings(env={}, config_path=path, profile="prod")
    assert (flag.url, flag.token, flag.domain, flag.timeout) == (
        "https://prod.test",
        "tok_prod",
        "prod-domain",
        5.0,
    )
    from_env = load_settings(env={"LOREBASE_PROFILE": "prod"}, config_path=path)
    assert from_env.profile == "prod"


def test_precedence_flag_then_environment_then_file(tmp_path: Path) -> None:
    path = write(tmp_path)
    env = {
        "LOREBASE_URL": "http://env.test",
        "LOREBASE_API_KEY": "lbk_env",
        "LOREBASE_DOMAIN": "env-domain",
        "LOREBASE_BATCH_SIZE": "5",
        "LOREBASE_LIST_LIMIT": "9",
    }
    from_env = load_settings(env=env, config_path=path)
    assert from_env.url == "http://env.test"
    assert from_env.api_key == "lbk_env"
    assert from_env.domain == "env-domain"
    assert (from_env.batch_size, from_env.list_limit) == (5, 9)
    flagged = load_settings(env=env, config_path=path, url="http://flag.test")
    assert flagged.url == "http://flag.test"


def test_config_path_can_come_from_the_environment(tmp_path: Path) -> None:
    path = write(tmp_path)
    settings = load_settings(env={"LOREBASE_CONFIG": str(path)})
    assert settings.url == "http://local.test"
    assert settings.config_path == path


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("profile = 'ghost'\n", "profile 'ghost'"),
        ("[import]\nbatch_size = 0\n", "batch_size must be a positive integer"),
        ("[output]\nlist_limit = 'many'\n", "list_limit must be a positive integer"),
        ("this is = not toml [", "cannot read config file"),
    ],
)
def test_bad_configuration_is_a_clear_error(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(CliError, match=message):
        load_settings(env={}, config_path=write(tmp_path, text))


def test_missing_named_config_file_is_an_error_but_a_missing_default_is_not(
    tmp_path: Path,
) -> None:
    with pytest.raises(CliError, match="not found"):
        load_settings(env={}, config_path=tmp_path / "nope.toml")
    assert load_settings(env={"XDG_CONFIG_HOME": str(tmp_path)}).url is None


def test_credentials_in_a_shared_file_warn(tmp_path: Path) -> None:
    warnings: list[str] = []
    load_settings(env={}, config_path=write(tmp_path, mode=0o644), warn=warnings.append)
    assert len(warnings) == 1
    assert "chmod 600" in warnings[0]
    private: list[str] = []
    load_settings(env={}, config_path=write(tmp_path, mode=0o600), warn=private.append)
    assert private == []
    overridden: list[str] = []  # credentials from the environment do not come from the file
    load_settings(
        env={"LOREBASE_API_KEY": "lbk_env"},
        config_path=write(tmp_path, "[profiles.a]\napi_key='x'\n", mode=0o644),
        profile="a",
        warn=overridden.append,
    )
    assert overridden == []
