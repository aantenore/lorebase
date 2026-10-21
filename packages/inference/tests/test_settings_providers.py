from __future__ import annotations

import pytest
from pydantic import ValidationError

from lorebase_inference.settings import (
    EmbeddingSettings,
    InferenceSettings,
    LlmSettings,
    LocalExtractorSettings,
    RetrySettings,
)


def test_documented_defaults() -> None:
    settings = InferenceSettings()
    assert settings.llm.base_url == "http://localhost:11434/v1"
    assert settings.llm.structured_output == "json_schema"
    assert settings.llm.repair_attempts == 1
    assert settings.llm.retry == RetrySettings(max_retries=3, backoff_base_s=0.5, backoff_max_s=30)
    assert settings.embeddings.provider == "fastembed"
    assert settings.embeddings.batch_size == 32
    assert settings.embeddings.cache_dir is None
    assert settings.local_extractors.provider == "gliner"
    assert settings.local_extractors.relation_provider == "glirel"
    assert settings.local_extractors.gliner.window_chars == 1500


def test_fake_provider_selects_the_offline_relation_provider() -> None:
    assert LocalExtractorSettings(provider="fake").relation_provider == "fake"
    explicit = LocalExtractorSettings(provider="fake", relation_provider="cooccurrence")
    assert explicit.relation_provider == "cooccurrence"
    assert LocalExtractorSettings().relation_provider == "glirel"


def test_task_override_inherits_the_retry_policy_and_repair_attempts() -> None:
    llm = LlmSettings.model_validate(
        {
            "retry": {"max_retries": 5},
            "repair_attempts": 2,
            "tasks": {"extraction": {"model": "tiny"}},
        }
    )
    effective = llm.for_task("extraction")
    assert (effective.model, effective.retry.max_retries, effective.repair_attempts) == (
        "tiny",
        5,
        2,
    )


def test_a_task_on_another_endpoint_does_not_inherit_the_api_key() -> None:
    # The main key belongs to the main endpoint; a task routed elsewhere without its own key
    # must not send it to that other host.
    llm = LlmSettings.model_validate(
        {
            "base_url": "https://main.example/v1",
            "api_key": "main-key-123",
            "tasks": {
                "elsewhere": {"base_url": "https://other.example/v1"},
                "same": {"model": "tiny"},
            },
        }
    )
    assert llm.for_task("elsewhere").api_key is None
    same = llm.for_task("same").api_key
    assert same is not None
    assert same.get_secret_value() == "main-key-123"


@pytest.mark.parametrize(
    "section",
    [
        {"llm": {"repair_attempts": 4}},
        {"llm": {"retry": {"max_retries": -1}}},
        {"llm": {"retry": {"backoff_max_s": 0}}},
        {"embeddings": {"batch_size": 0}},
        {"llm": {"max_tokens": 0}},
    ],
)
def test_out_of_range_values_are_rejected(section) -> None:
    with pytest.raises(ValidationError):
        InferenceSettings.model_validate(section)


def test_remote_embeddings_need_an_endpoint() -> None:
    with pytest.raises(ValidationError, match="base_url"):
        EmbeddingSettings(provider="openai_compatible")
