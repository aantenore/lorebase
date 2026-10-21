from __future__ import annotations

import pytest

from lorebase_inference import (
    ChatModel,
    EmbeddingModel,
    InferenceSettings,
    Message,
    ProviderRegistry,
    ProviderUnavailableError,
    RelationExtractor,
    SpanExtractor,
    UnknownProviderError,
    create_chat_model,
    create_embedding_model,
    create_relation_extractor,
    create_span_extractor,
    validate_providers,
)
from lorebase_inference import registry as registry_module
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
    ("factory", "section", "cls"),
    [
        (create_chat_model, {"llm": {"provider": "openai_compatible"}}, "EndpointChatModel"),
        (
            create_embedding_model,
            {"embeddings": {"provider": "fastembed"}},
            "LocalEmbeddingModel",
        ),
        (
            create_embedding_model,
            {"embeddings": {"provider": "openai_compatible", "base_url": "http://e/v1"}},
            "EndpointEmbeddingModel",
        ),
        (create_span_extractor, {"local_extractors": {"provider": "gliner"}}, "LocalSpanExtractor"),
        (
            create_relation_extractor,
            {"local_extractors": {"relation_provider": "glirel"}},
            "LocalRelationExtractor",
        ),
        (
            create_relation_extractor,
            {"local_extractors": {"relation_provider": "cooccurrence"}},
            "CooccurrenceRelationExtractor",
        ),
        (
            create_relation_extractor,
            {"local_extractors": {"relation_provider": "fake"}},
            "CooccurrenceRelationExtractor",
        ),
    ],
)
def test_every_provider_is_wired_and_loads_models_lazily(
    factory, section, cls, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pretend the optional libraries are installed: creating a provider must not import them.
    monkeypatch.setattr(registry_module.importlib.util, "find_spec", lambda name: object())
    assert type(factory(InferenceSettings.model_validate(section))).__name__ == cls


@pytest.mark.parametrize(
    ("factory", "section", "extra"),
    [
        (create_embedding_model, {"embeddings": {"provider": "fastembed"}}, "fastembed"),
        (create_span_extractor, {"local_extractors": {"provider": "gliner"}}, "local-extractors"),
        (
            create_relation_extractor,
            {"local_extractors": {"relation_provider": "glirel"}},
            "local-extractors",
        ),
    ],
)
def test_missing_extra_fails_with_an_install_hint(
    factory, section, extra, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry_module.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(ProviderUnavailableError, match=rf"lorebase-inference\[{extra}\]"):
        factory(InferenceSettings.model_validate(section))


def test_startup_validation_reports_missing_extras_only_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry_module.importlib.util, "find_spec", lambda name: None)
    settings = InferenceSettings()  # defaults: fastembed, gliner, glirel

    validate_providers(settings)
    with pytest.raises(ProviderUnavailableError) as caught:
        validate_providers(settings, require_installed=True)
    message = str(caught.value)
    for module in ("fastembed", "gliner", "glirel"):
        assert module in message
    assert message.count("lorebase-inference[local-extractors]") == 2

    fake = fake_settings()
    validate_providers(fake, require_installed=True)


def test_fallback_relation_provider_needs_no_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module.importlib.util, "find_spec", lambda name: None)
    settings = InferenceSettings.model_validate(
        {
            "embeddings": {"provider": "fake"},
            "local_extractors": {"provider": "fake", "relation_provider": "cooccurrence"},
            "llm": {"provider": "fake"},
        }
    )
    validate_providers(settings, require_installed=True)
    assert isinstance(create_relation_extractor(settings), RelationExtractor)


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
    registry.register_relation_extractor("fake", lambda s: create_relation_extractor(s))
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
