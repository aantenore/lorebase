"""Provider selection from configuration: the only place that names providers.

Chat providers: `openai_compatible`, `fake`. Embedding providers: `fastembed`,
`openai_compatible`, `fake`. Span extractor providers: `gliner`, `fake`. The real providers are
registered in M4; until then selecting them fails with an explicit message.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn

from lorebase_inference.fake import FakeChatModel, FakeEmbeddingModel, FakeSpanExtractor
from lorebase_inference.interfaces import ChatModel, EmbeddingModel, SpanExtractor
from lorebase_inference.settings import InferenceSettings, LlmSettings

ChatFactory = Callable[[InferenceSettings, LlmSettings], ChatModel]
EmbeddingFactory = Callable[[InferenceSettings], EmbeddingModel]
SpanFactory = Callable[[InferenceSettings], SpanExtractor]

AVAILABLE_IN = "M4"


class UnknownProviderError(ValueError):
    """The configuration names a provider that is not registered."""


class ProviderRegistry:
    """Maps provider names to factories for each model kind."""

    def __init__(self) -> None:
        self._chat: dict[str, ChatFactory] = {}
        self._embedding: dict[str, EmbeddingFactory] = {}
        self._span: dict[str, SpanFactory] = {}

    def register_chat(self, name: str, factory: ChatFactory) -> None:
        self._chat[name] = factory

    def register_embedding(self, name: str, factory: EmbeddingFactory) -> None:
        self._embedding[name] = factory

    def register_span_extractor(self, name: str, factory: SpanFactory) -> None:
        self._span[name] = factory

    def validate(self, settings: InferenceSettings) -> None:
        """Fail if any configured provider, including per-task ones, is not registered."""
        llm = settings.llm
        for task in (None, *llm.tasks):
            _lookup(self._chat, "chat", llm.for_task(task).provider)
        _lookup(self._embedding, "embedding", settings.embeddings.provider)
        _lookup(self._span, "span extractor", settings.local_extractors.provider)

    def create_chat_model(self, settings: InferenceSettings, task: str | None = None) -> ChatModel:
        """Chat model for `task` (per-task overrides applied), or the default one."""
        llm = settings.llm.for_task(task)
        return _lookup(self._chat, "chat", llm.provider)(settings, llm)

    def create_embedding_model(self, settings: InferenceSettings) -> EmbeddingModel:
        """Embedding model of the configured provider."""
        return _lookup(self._embedding, "embedding", settings.embeddings.provider)(settings)

    def create_span_extractor(self, settings: InferenceSettings) -> SpanExtractor:
        """Tier-1 span extractor of the configured provider."""
        return _lookup(self._span, "span extractor", settings.local_extractors.provider)(settings)


def _lookup[F](table: dict[str, F], kind: str, name: str) -> F:
    try:
        return table[name]
    except KeyError:
        known = ", ".join(sorted(table)) or "none"
        raise UnknownProviderError(
            f"unknown {kind} provider {name!r}; available: {known}"
        ) from None


def _unavailable(kind: str, name: str) -> Callable[..., NoReturn]:
    def factory(*_args: object) -> NoReturn:
        raise NotImplementedError(
            f"{kind} provider {name!r} is available in {AVAILABLE_IN}; "
            "use provider 'fake' until then"
        )

    return factory


def build_default_registry() -> ProviderRegistry:
    """Registry with the fake provider implemented and real providers reserved for M4."""
    registry = ProviderRegistry()
    registry.register_chat("fake", lambda s, llm: FakeChatModel(llm, s.fake))
    registry.register_chat("openai_compatible", _unavailable("chat", "openai_compatible"))
    registry.register_embedding("fake", lambda s: FakeEmbeddingModel(s.embeddings))
    registry.register_embedding("fastembed", _unavailable("embedding", "fastembed"))
    registry.register_embedding("openai_compatible", _unavailable("embedding", "openai_compatible"))
    registry.register_span_extractor("fake", lambda s: FakeSpanExtractor(s.fake.dictionary))
    registry.register_span_extractor("gliner", _unavailable("span extractor", "gliner"))
    return registry


default_registry = build_default_registry()
create_chat_model = default_registry.create_chat_model
create_embedding_model = default_registry.create_embedding_model
create_span_extractor = default_registry.create_span_extractor
validate_providers = default_registry.validate
