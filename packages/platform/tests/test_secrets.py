from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from lorebase_platform.secrets import (
    SecretResolutionError,
    SecretValue,
    registered_secret_values,
    resolve_secret,
)
from lorebase_platform.settings import (
    CONFIG_ENV_VAR,
    ConfigError,
    SectionModel,
    load_settings,
    register_section,
)


class Holder(SectionModel):
    password: SecretValue
    optional: SecretValue | None = None


def test_env_reference_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_PASSWORD", "s3cr3t-value")
    assert resolve_secret("secret://env/DEMO_PASSWORD") == "s3cr3t-value"


def test_file_reference_resolves_and_strips_newline(tmp_path: Path) -> None:
    path = tmp_path / "pw"
    path.write_text("file-secret\n")
    assert resolve_secret(f"secret://file/{path}") == "file-secret"


def test_literal_value_is_returned_unchanged() -> None:
    assert resolve_secret("plain-literal") == "plain-literal"


def test_missing_environment_variable_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEMO_MISSING", raising=False)
    with pytest.raises(SecretResolutionError, match=r"secret://env/DEMO_MISSING.*not set"):
        resolve_secret("secret://env/DEMO_MISSING")


def test_missing_file_is_a_clear_error() -> None:
    with pytest.raises(SecretResolutionError, match="cannot read secret file"):
        resolve_secret("secret://file//nonexistent/secret")


def test_unsupported_provider_and_malformed_reference() -> None:
    with pytest.raises(SecretResolutionError, match="unsupported secret provider 'vault'"):
        resolve_secret("secret://vault/path")
    with pytest.raises(SecretResolutionError, match="malformed"):
        resolve_secret("secret://env")


def test_empty_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_EMPTY", "")
    with pytest.raises(SecretResolutionError, match="empty"):
        resolve_secret("secret://env/DEMO_EMPTY")


def test_secret_fields_hide_values_in_repr_and_str(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_PASSWORD", "s3cr3t-value")
    holder = Holder(password="secret://env/DEMO_PASSWORD")
    assert isinstance(holder.password, SecretStr)
    assert holder.password.get_secret_value() == "s3cr3t-value"
    assert "s3cr3t-value" not in repr(holder)
    assert "s3cr3t-value" not in str(holder)
    assert "s3cr3t-value" not in holder.model_dump_json()


def test_resolved_values_are_registered_for_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_PASSWORD", "s3cr3t-value")
    Holder(password="secret://env/DEMO_PASSWORD")
    assert "s3cr3t-value" in registered_secret_values()


def test_missing_reference_in_settings_names_key_and_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_section("holder", Holder)
    config = tmp_path / "c.yaml"
    config.write_text("holder:\n  password: secret://env/DEMO_ABSENT\n")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(config))
    monkeypatch.delenv("DEMO_ABSENT", raising=False)
    with pytest.raises(ConfigError) as caught:
        load_settings()
    assert "holder.password" in str(caught.value)
    assert "secret://env/DEMO_ABSENT" in str(caught.value)


def test_errors_never_contain_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    register_section("holder", Holder)
    monkeypatch.setenv("LOREBASE__HOLDER__PASSWORD", "super-secret-literal")
    monkeypatch.setenv("LOREBASE__HOLDER__OPTIONAL", "[1, 2]")
    with pytest.raises(ConfigError) as caught:
        load_settings()
    assert "super-secret-literal" not in str(caught.value)
