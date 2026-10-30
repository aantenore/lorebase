from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from lorebase_platform import secrets
from lorebase_platform.secrets import (
    SecretResolutionError,
    SecretValue,
    register_secret_provider,
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


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Providers registered by a test are forgotten afterwards."""
    monkeypatch.setattr(secrets, "_PROVIDERS", dict(secrets._PROVIDERS))


@pytest.mark.usefixtures("providers")
def test_a_registered_provider_resolves_its_references() -> None:
    vault = {"db/password": "vault-s3cr3t"}

    def lookup(target: str) -> str:
        if target not in vault:
            raise SecretResolutionError(f"no secret at {target!r}")
        return vault[target]

    register_secret_provider("vault", lookup)
    assert resolve_secret("secret://vault/db/password") == "vault-s3cr3t"
    assert "vault-s3cr3t" in registered_secret_values()
    with pytest.raises(SecretResolutionError, match="no secret at 'missing'"):
        resolve_secret("secret://vault/missing")


@pytest.mark.usefixtures("providers")
def test_providers_are_not_replaced_silently() -> None:
    register_secret_provider("vault", lambda target: "first")
    with pytest.raises(ValueError, match="already registered"):
        register_secret_provider("vault", lambda target: "second")
    register_secret_provider("vault", lambda target: "second", replace=True)
    assert resolve_secret("secret://vault/x") == "second"
    for built_in in ("env", "file"):
        with pytest.raises(ValueError, match="cannot be replaced"):
            register_secret_provider(built_in, lambda target: "x", replace=True)


@pytest.mark.usefixtures("providers")
@pytest.mark.parametrize("name", ["", "Vault", "my vault", "a/b", "vault:"])
def test_provider_names_must_be_usable_in_references(name: str) -> None:
    with pytest.raises(ValueError, match="invalid secret provider name"):
        register_secret_provider(name, lambda target: "x")
