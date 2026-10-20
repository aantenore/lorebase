from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import Field

from lorebase_platform.secrets import SecretValue
from lorebase_platform.settings import (
    CONFIG_ENV_VAR,
    ConfigError,
    SectionModel,
    Settings,
    load_settings,
    register_section,
)

EXAMPLE = Path(__file__).resolve().parents[3] / "config" / "lorebase.example.yaml"


class DemoSection(SectionModel):
    count: int = 1
    name: str = "base"
    token: SecretValue | None = None
    tags: list[str] = Field(default_factory=list)


def write_yaml(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_defaults_apply_without_any_source() -> None:
    settings = load_settings()
    assert settings.telemetry.otlp_endpoint is None
    assert settings.telemetry.sample_ratio == 0.1
    assert settings.logging.level == "INFO"


def test_yaml_overrides_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_yaml(tmp_path, "telemetry:\n  sample_ratio: 0.5\n  service_name: from-yaml\n")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(path))
    settings = load_settings()
    assert settings.telemetry.sample_ratio == 0.5
    assert settings.telemetry.service_name == "from-yaml"
    assert settings.telemetry.service_namespace == "lorebase"


def test_env_overrides_yaml_overrides_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_yaml(tmp_path, "telemetry:\n  sample_ratio: 0.5\n  service_name: from-yaml\n")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(path))
    monkeypatch.setenv("LOREBASE__TELEMETRY__SAMPLE_RATIO", "0.25")
    settings = load_settings()
    assert settings.telemetry.sample_ratio == 0.25  # env wins
    assert settings.telemetry.service_name == "from-yaml"  # yaml wins over default
    assert settings.telemetry.service_namespace == "lorebase"  # default


def test_env_accepts_json_for_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOREBASE__LOGGING__REDACT_KEYS", '["pin", "otp"]')
    assert load_settings().logging.redact_keys == ("pin", "otp")


def test_empty_endpoint_means_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOREBASE__TELEMETRY__OTLP_ENDPOINT", "  ")
    assert load_settings().telemetry.otlp_endpoint is None


def test_invalid_value_names_key_and_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOREBASE__TELEMETRY__SAMPLE_RATIO", "3")
    monkeypatch.setenv("LOREBASE__LOGGING__LEVEL", "LOUD")
    with pytest.raises(ConfigError) as caught:
        load_settings()
    message = str(caught.value)
    assert "telemetry.sample_ratio: Input should be less than or equal to 1" in message
    assert "logging.level" in message


def test_unknown_key_in_owned_section_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, str(write_yaml(tmp_path, "telemetry:\n  endpoint: x\n")))
    with pytest.raises(ConfigError, match=r"telemetry\.endpoint: Extra inputs"):
        load_settings()


def test_missing_config_file_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, "/nonexistent/lorebase.yaml")
    with pytest.raises(ConfigError, match=CONFIG_ENV_VAR):
        load_settings()


def test_yaml_must_be_a_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, str(write_yaml(tmp_path, "- a\n- b\n")))
    with pytest.raises(ConfigError, match="top level must be a mapping"):
        load_settings()


def test_registered_section_is_layered_and_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_section("demo", DemoSection)
    path = write_yaml(tmp_path, "demo:\n  count: 5\n  name: yaml\n  tags: [a]\n")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(path))
    monkeypatch.setenv("LOREBASE__DEMO__COUNT", "7")
    section = load_settings().section("demo", DemoSection)
    assert (section.count, section.name, section.tags) == (7, "yaml", ["a"])


def test_registered_section_errors_use_the_section_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_section("demo", DemoSection)
    monkeypatch.setenv("LOREBASE__DEMO__COUNT", "many")
    with pytest.raises(ConfigError, match=r"demo\.count"):
        load_settings()


def test_unregistered_sections_are_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, str(write_yaml(tmp_path, "database:\n  pool_size: 3\n")))
    assert load_settings().model_extra == {"database": {"pool_size": 3}}


def test_env_aliases_override_yaml_but_not_explicit_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_section("demo", DemoSection, env_aliases={"DEMO_NAME": "name", "DEMO_TOKEN": "token"})
    monkeypatch.setenv(CONFIG_ENV_VAR, str(write_yaml(tmp_path, "demo:\n  name: yaml\n")))
    monkeypatch.setenv("DEMO_NAME", "alias")
    monkeypatch.setenv("DEMO_TOKEN", "")
    assert load_settings().section("demo", DemoSection).name == "alias"
    assert load_settings().section("demo", DemoSection).token is None  # empty alias ignored
    monkeypatch.setenv("LOREBASE__DEMO__NAME", "explicit")
    assert load_settings().section("demo", DemoSection).name == "explicit"


def test_conflicting_env_keys_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOREBASE__DEMO", "x")
    monkeypatch.setenv("LOREBASE__DEMO__COUNT", "1")
    with pytest.raises(ConfigError, match="conflicts"):
        load_settings()


def test_registering_a_platform_section_name_is_refused() -> None:
    with pytest.raises(ValueError, match="owned by lorebase_platform"):
        register_section("telemetry", DemoSection)


def test_example_file_loads_and_matches_code_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = load_settings()
    monkeypatch.setenv(CONFIG_ENV_VAR, str(EXAMPLE))
    from_example = load_settings()
    assert from_example.telemetry == defaults.telemetry
    assert from_example.logging == defaults.logging


def test_settings_is_immutable() -> None:
    settings = load_settings()
    with pytest.raises(ValueError, match="frozen"):
        settings.telemetry = settings.telemetry.model_copy()
    assert isinstance(settings, Settings)
