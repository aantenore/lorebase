from __future__ import annotations

import pytest

from lorebase_inference import (
    ChatModel,
    EmbeddingModel,
    InferenceSettings,
    Message,
    ProviderRegistry,
    SpanExtractor,
    UnknownProviderError,
    create_chat_model,
    create_embedding_model,
    create_span_extractor,
    validate_providers,
)
from lorebase_inference.fake import FakeChatModel
from lorebase_inference.registry import build_default_registry
from lorebase_inference.settings import LlmTaskOverride


def fake_settings(**llm: object) -> InferenceSettings:
    return InferenceSettings.model_validate(
        {
            "llm": {"provider": "fake", **llm},
            "embeddings": {"provider": "fake", "dims": 16},
            "local_extractors": {"provider": "fake"},
        }
    )


async def test_fake_provider_is_selected_by_configuration_alone() -> None:
    settings = fake_settings()
    chat = create_chat_model(settings)
    embedder = create_embedding_model(settings)
    extractor = create_span_extractor(settings)

    assert isinstance(chat, ChatModel)
    assert isinstance(embedder, EmbeddingModel)
    assert isinstance(extractor, SpanExtractor)
    assert embedder.dims == 16
    assert (await chat.complete_json([Message("user", "hi")], {"type": "string"})).data == "hi"


@pytest.mark.parametrize(
    ("factory", "section", "name"),
    [
        (create_chat_model, {"llm": {"provider": "openai_compatible"}}, "openai_compatible"),
        (create_embedding_model, {"embeddings": {"provider": "fastembed"}}, "fastembed"),
        (
            create_embedding_model,
            {"embeddings": {"provider": "openai_compatible", "base_url": "http://e/v1"}},
            "openai_compatible",
        ),
        (create_span_extractor, {"local_extractors": {"provider": "gliner"}}, "gliner"),
    ],
)
def test_real_providers_are_reserved_for_m4(factory, section, name) -> None:
    with pytest.raises(NotImplementedError) as caught:
        factory(InferenceSettings.model_validate(section))
    assert "available in M4" in str(caught.value)
    assert name in str(caught.value)


def test_unknown_provider_names_the_available_ones() -> None:
    registry = ProviderRegistry()
    registry.register_chat("only", lambda s, llm: FakeChatModel(llm, s.fake))
    with pytest.raises(UnknownProviderError, match=r"'fake'.*available: only"):
        registry.create_chat_model(fake_settings())


def test_validate_checks_configured_and_per_task_providers() -> None:
    registry = ProviderRegistry()
    registry.register_chat("fake", lambda s, llm: FakeChatModel(llm, s.fake))
    registry.register_embedding("fake", lambda s: create_embedding_model(s))
    registry.register_span_extractor("fake", lambda s: create_span_extractor(s))
    registry.validate(fake_settings())

    with_task = fake_settings(tasks={"extraction": LlmTaskOverride(provider="openai_compatible")})
    with pytest.raises(UnknownProviderError, match="openai_compatible"):
        registry.validate(with_task)
    build_default_registry().validate(with_task)


async def test_per_task_override_selects_its_own_model() -> None:
    settings = fake_settings(
        model="base", tasks={"extraction": LlmTaskOverride(model="small-local")}
    )
    result = await create_chat_model(settings, task="extraction").complete_json(
        [Message("user", "x")], {"type": "string"}
    )
    assert result.model == "fake:small-local"
    base = await create_chat_model(settings).complete_json(
        [Message("user", "x")], {"type": "string"}
    )
    assert base.model == "fake:base"


def test_default_configuration_validates() -> None:
    validate_providers(InferenceSettings())
