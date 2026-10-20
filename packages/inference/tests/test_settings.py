from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from lorebase_inference.settings import InferenceSettings, inference_settings
from lorebase_platform.settings import CONFIG_ENV_VAR, ConfigError, load_settings

EXAMPLE = Path(__file__).resolve().parents[3] / "config" / "lorebase.example.yaml"


def config(tmp_path: Path, text: str) -> str:
    path = tmp_path / "lorebase.yaml"
    path.write_text(text)
    return str(path)


def test_defaults_are_local() -> None:
    settings = inference_settings(load_settings())
    assert settings.llm.provider == "openai_compatible"
    assert settings.llm.base_url == "http://localhost:11434/v1"
    assert settings.llm.api_key is None
    assert settings.embeddings.provider == "fastembed"
    assert settings.embeddings.dims == 384


def test_env_aliases_override_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    yaml_text = "inference:\n  llm:\n    base_url: http://yaml:1/v1\n    model: yaml-model\n"
    monkeypatch.setenv(CONFIG_ENV_VAR, config(tmp_path, yaml_text))
    monkeypatch.setenv("LLM_BASE_URL", "http://alias:2/v1")
    monkeypatch.setenv("LLM_MODEL", "alias-model")
    llm = inference_settings(load_settings()).llm
    assert (llm.base_url, llm.model) == ("http://alias:2/v1", "alias-model")


def test_explicit_env_key_beats_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "alias-model")
    monkeypatch.setenv("LOREBASE__INFERENCE__LLM__MODEL", "explicit-model")
    assert inference_settings(load_settings()).llm.model == "explicit-model"


def test_api_key_is_optional_and_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    assert inference_settings(load_settings()).llm.api_key is None
    monkeypatch.setenv("LLM_API_KEY", "sk-live-1234567890")
    settings = inference_settings(load_settings())
    assert isinstance(settings.llm.api_key, SecretStr)
    assert settings.llm.api_key.get_secret_value() == "sk-live-1234567890"
    assert "sk-live-1234567890" not in repr(settings)


def test_api_key_accepts_secret_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSTED_KEY", "from-reference")
    monkeypatch.setenv("LLM_API_KEY", "secret://env/HOSTED_KEY")
    key = inference_settings(load_settings()).llm.api_key
    assert key is not None
    assert key.get_secret_value() == "from-reference"


def test_missing_secret_reference_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ABSENT_KEY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "secret://env/ABSENT_KEY")
    with pytest.raises(ConfigError, match=r"inference\.llm\.api_key.*ABSENT_KEY"):
        load_settings()


def test_unknown_provider_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOREBASE__INFERENCE__LLM__PROVIDER", "acme")
    with pytest.raises(ConfigError, match=r"inference\.llm\.provider"):
        load_settings()


def test_openai_compatible_embeddings_require_an_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOREBASE__INFERENCE__EMBEDDINGS__PROVIDER", "openai_compatible")
    with pytest.raises(ConfigError, match=r"inference\.embeddings.*base_url is required"):
        load_settings()


def test_task_override_changes_only_the_given_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_text = """
inference:
  llm:
    model: small
    api_key: base-key-123
    tasks:
      schema_discovery:
        base_url: https://hosted.example/v1
        model: large
        api_key: task-key-456
        price_per_million_tokens: {input: 1, output: 2}
"""
    monkeypatch.setenv(CONFIG_ENV_VAR, config(tmp_path, yaml_text))
    llm = inference_settings(load_settings()).llm

    default = llm.for_task(None)
    assert default.model == "small"
    assert llm.for_task("unknown_task").model == "small"

    task = llm.for_task("schema_discovery")
    assert task.model == "large"
    assert task.base_url == "https://hosted.example/v1"
    assert task.api_key is not None
    assert task.api_key.get_secret_value() == "task-key-456"
    assert task.price_per_million_tokens.output == 2
    assert task.temperature == llm.temperature
    assert task.tasks == {}


def test_example_file_matches_code_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = inference_settings(load_settings())
    monkeypatch.setenv(CONFIG_ENV_VAR, str(EXAMPLE))
    assert inference_settings(load_settings()) == defaults
    assert isinstance(defaults, InferenceSettings)
