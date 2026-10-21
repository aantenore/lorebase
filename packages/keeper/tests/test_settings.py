from __future__ import annotations

from pathlib import Path

import pytest

from lorebase_keeper.settings import KeeperSettings, keeper_settings
from lorebase_platform.settings import CONFIG_ENV_VAR, ConfigError, Settings, load_settings

EXAMPLE = Path(__file__).resolve().parents[3] / "config" / "lorebase.example.yaml"


def test_defaults() -> None:
    keeper = keeper_settings(load_settings())
    assert keeper.host == "127.0.0.1"
    assert keeper.port == 8080
    assert keeper.api_prefix == "/api/v1"
    assert keeper.cors.allow_origins == ()
    assert keeper.auth.mode == "none"


def test_environment_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOREBASE__KEEPER__PORT", "9100")
    monkeypatch.setenv("LOREBASE__KEEPER__CORS__ALLOW_ORIGINS", '["https://app.example.com"]')
    keeper = keeper_settings(load_settings())
    assert keeper.port == 9100
    assert keeper.cors.allow_origins == ("https://app.example.com",)


def test_example_file_matches_code_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, str(EXAMPLE))
    assert keeper_settings(load_settings()) == KeeperSettings()


@pytest.mark.parametrize(
    ("section", "needle"),
    [
        ({"port": 70000}, "keeper.port"),
        ({"api_prefix": "api/v1"}, "keeper.api_prefix"),
        ({"api_prefix": "/api/v1/"}, "keeper.api_prefix"),
        ({"cors": {"allow_origins": ["*"], "allow_credentials": True}}, "keeper.cors"),
        ({"pagination": {"default_limit": 600, "max_limit": 500}}, "keeper.pagination"),
        ({"limits": {"max_request_body_bytes": 0}}, "keeper.limits.max_request_body_bytes"),
        ({"unknown": 1}, "keeper.unknown"),
    ],
)
def test_invalid_values_name_the_key(section: dict[str, object], needle: str) -> None:
    with pytest.raises(ConfigError) as caught:
        keeper_settings(Settings(keeper=section))
    assert needle in str(caught.value)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5", "keeper.internal"])
def test_auth_none_refuses_a_non_loopback_bind(host: str) -> None:
    with pytest.raises(ConfigError) as caught:
        keeper_settings(Settings(keeper={"host": host, "auth": {"mode": "none"}}))
    assert "allow_insecure" in str(caught.value)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_auth_none_accepts_a_loopback_bind(host: str) -> None:
    assert keeper_settings(Settings(keeper={"host": host})).auth.mode == "none"


def test_auth_none_on_a_public_bind_needs_the_explicit_flag() -> None:
    keeper = keeper_settings(
        Settings(keeper={"host": "0.0.0.0", "auth": {"mode": "none", "allow_insecure": True}})
    )
    assert keeper.auth.allow_insecure


def test_authenticated_modes_may_bind_anywhere() -> None:
    keeper = keeper_settings(
        Settings(
            keeper={
                "host": "0.0.0.0",
                "auth": {"mode": "api_key", "api_keys": {"pepper": "p" * 32}},
            }
        )
    )
    assert keeper.auth.mode == "api_key"
