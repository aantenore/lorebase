"""Provider selection from configuration: the only place that names providers.

Chat providers: `openai_compatible`, `fake`. Embedding providers: `fastembed`,
`openai_compatible`, `fake`. Span extractor providers: `gliner`, `fake`. Relation extractor
providers: `glirel`, `cooccurrence`, `fake`.

The local model libraries are optional extras (`fastembed`, `local-extractors`). They are
imported lazily, only when a provider that needs them is created, and the small backend
functions below are the only code that calls them; everything else in the package works with
plain callables. Missing extras fail with an install hint, at `validate_providers(...,
require_installed=True)` or at creation.
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lorebase_inference.chat import EndpointChatModel
from lorebase_inference.errors import ProviderUnavailableError
from lorebase_inference.fake import FakeChatModel, FakeEmbeddingModel, FakeSpanExtractor
from lorebase_inference.interfaces import (
    ChatModel,
    EmbeddingModel,
    RelationExtractor,
    SpanExtractor,
)
from lorebase_inference.local_embeddings import EmbedBackend, LocalEmbeddingModel
from lorebase_inference.local_relations import (
    CooccurrenceRelationExtractor,
    LocalRelationExtractor,
    RelationBackend,
)
from lorebase_inference.local_spans import LocalSpanExtractor, SpanBackend
from lorebase_inference.remote_embeddings import EndpointEmbeddingModel
from lorebase_inference.settings import (
    EmbeddingSettings,
    ExtractorSettings,
    InferenceSettings,
    LlmSettings,
)

ChatFactory = Callable[[InferenceSettings, LlmSettings], ChatModel]
EmbeddingFactory = Callable[[InferenceSettings], EmbeddingModel]
SpanFactory = Callable[[InferenceSettings], SpanExtractor]
RelationFactory = Callable[[InferenceSettings], RelationExtractor]


class UnknownProviderError(ValueError):
    """The configuration names a provider that is not registered."""


@dataclass(frozen=True, slots=True)
class Requirement:
    """Optional Python modules a provider imports, and the extra that installs them."""

    modules: tuple[str, ...]
    extra: str

    def missing(self) -> list[str]:
        """Modules that cannot be imported."""
        return [m for m in self.modules if importlib.util.find_spec(m) is None]

    def ensure(self, provider: str) -> None:
        """Raise `ProviderUnavailableError` with an install hint when a module is missing."""
        missing = self.missing()
        if missing:
            raise ProviderUnavailableError(
                f"provider {provider!r} needs {', '.join(missing)}, which is not installed; "
                f"install it with: pip install 'lorebase-inference[{self.extra}]'"
            )


@dataclass(frozen=True, slots=True)
class _Entry[F]:
    factory: F
    requires: Requirement | None = None


class ProviderRegistry:
    """Maps provider names to factories for each model kind."""

    def __init__(self) -> None:
        self._chat: dict[str, _Entry[ChatFactory]] = {}
        self._embedding: dict[str, _Entry[EmbeddingFactory]] = {}
        self._span: dict[str, _Entry[SpanFactory]] = {}
        self._relation: dict[str, _Entry[RelationFactory]] = {}

    def register_chat(
        self, name: str, factory: ChatFactory, requires: Requirement | None = None
    ) -> None:
        self._chat[name] = _Entry(factory, requires)

    def register_embedding(
        self, name: str, factory: EmbeddingFactory, requires: Requirement | None = None
    ) -> None:
        self._embedding[name] = _Entry(factory, requires)

    def register_span_extractor(
        self, name: str, factory: SpanFactory, requires: Requirement | None = None
    ) -> None:
        self._span[name] = _Entry(factory, requires)

    def register_relation_extractor(
        self, name: str, factory: RelationFactory, requires: Requirement | None = None
    ) -> None:
        self._relation[name] = _Entry(factory, requires)

    def validate(self, settings: InferenceSettings, *, require_installed: bool = False) -> None:
        """Fail if any configured provider, including per-task ones, is not registered.

        With `require_installed`, also fail when a selected provider's optional dependency is
        missing (use at service start); without it the check is on names only.
        """
        llm = settings.llm
        selected: list[tuple[str, _Entry[Any]]] = []
        for task in (None, *llm.tasks):
            chat = llm.for_task(task).provider
            selected.append((chat, _lookup(self._chat, "chat", chat)))
        embedding = settings.embeddings.provider
        selected.append((embedding, _lookup(self._embedding, "embedding", embedding)))
        extractors = settings.local_extractors
        span = extractors.provider
        selected.append((span, _lookup(self._span, "span extractor", span)))
        relation = extractors.relation_provider
        selected.append((relation, _lookup(self._relation, "relation extractor", relation)))
        if require_installed:
            problems: dict[str, None] = {}
            for provider, entry in selected:
                try:
                    _ensure(entry, provider)
                except ProviderUnavailableError as exc:
                    problems[str(exc)] = None
            if problems:
                raise ProviderUnavailableError("; ".join(problems))

    def create_chat_model(self, settings: InferenceSettings, task: str | None = None) -> ChatModel:
        """Chat model for `task` (that task's overrides applied), or the default one.

        The default model routes `ChatOptions.task` to the matching override by itself.
        """
        llm = settings.llm.for_task(task) if task else settings.llm
        entry = _lookup(self._chat, "chat", llm.provider)
        _ensure(entry, llm.provider)
        return entry.factory(settings, llm)

    def create_embedding_model(self, settings: InferenceSettings) -> EmbeddingModel:
        """Embedding model of the configured provider."""
        name = settings.embeddings.provider
        entry = _lookup(self._embedding, "embedding", name)
        _ensure(entry, name)
        return entry.factory(settings)

    def create_span_extractor(self, settings: InferenceSettings) -> SpanExtractor:
        """Tier-1 span extractor of the configured provider."""
        name = settings.local_extractors.provider
        entry = _lookup(self._span, "span extractor", name)
        _ensure(entry, name)
        return entry.factory(settings)

    def create_relation_extractor(self, settings: InferenceSettings) -> RelationExtractor:
        """Tier-1 relation extractor of the configured provider."""
        name = settings.local_extractors.relation_provider
        entry = _lookup(self._relation, "relation extractor", name)
        _ensure(entry, name)
        return entry.factory(settings)


def _lookup[F](table: dict[str, _Entry[F]], kind: str, name: str) -> _Entry[F]:
    try:
        return table[name]
    except KeyError:
        known = ", ".join(sorted(table)) or "none"
        raise UnknownProviderError(
            f"unknown {kind} provider {name!r}; available: {known}"
        ) from None


def _ensure(entry: _Entry[Any], name: str) -> None:
    if entry.requires:
        entry.requires.ensure(name)


# ---- local model backends: the only code that calls the optional libraries -----------------


def _import(module: str) -> Any:
    return importlib.import_module(module)


def _fastembed_backend(embeddings: EmbeddingSettings) -> Callable[[], EmbedBackend]:
    """Factory of a CPU ONNX embedding backend; the model is loaded when first needed."""

    def build() -> EmbedBackend:
        model = _import("fastembed").TextEmbedding(
            model_name=embeddings.model,
            cache_dir=str(embeddings.cache_dir) if embeddings.cache_dir else None,
            threads=embeddings.threads,
            providers=["CPUExecutionProvider"],
        )

        def embed(texts: Sequence[str]) -> list[list[float]]:
            return [vector.tolist() for vector in model.embed(list(texts), batch_size=len(texts))]

        return embed

    return build


def _gliner_backend(extractor: ExtractorSettings) -> Callable[[], SpanBackend]:
    """Factory of a CPU zero-shot entity backend; the checkpoint is loaded when first needed."""

    def build() -> SpanBackend:
        model = _import("gliner").GLiNER.from_pretrained(
            extractor.model,
            cache_dir=str(extractor.cache_dir) if extractor.cache_dir else None,
            map_location="cpu",
        )

        def predict(text: str, labels: list[str], threshold: float) -> list[Mapping[str, Any]]:
            entities: list[Mapping[str, Any]] = model.predict_entities(
                text, labels, threshold=threshold
            )
            return entities

        return predict

    return build


def _glirel_backend(extractor: ExtractorSettings) -> Callable[[], RelationBackend]:
    """Factory of a CPU zero-shot relation backend; loaded when first needed."""

    def build() -> RelationBackend:
        glirel = _import("glirel").GLiREL
        cache = str(extractor.cache_dir) if extractor.cache_dir else None
        try:
            model = glirel.from_pretrained(extractor.model, cache_dir=cache)
        except TypeError:
            # glirel 1.2 with huggingface_hub 1.x: the hub mixin no longer passes two arguments
            # that the library's loader requires. Call the loader directly with their old values.
            model = glirel._from_pretrained(
                model_id=extractor.model,
                revision=None,
                cache_dir=cache,
                force_download=False,
                proxies=None,
                resume_download=False,
                local_files_only=False,
                token=None,
            )

        def predict(
            tokens: list[str], entities: list[list[Any]], labels: list[str], threshold: float
        ) -> list[Mapping[str, Any]]:
            relations: list[Mapping[str, Any]] = model.predict_relations(
                tokens, labels, threshold=threshold, ner=entities, top_k=-1
            )
            return relations

        return predict

    return build


EMBEDDINGS_EXTRA = Requirement(("fastembed",), "fastembed")
LOCAL_EXTRACTORS_EXTRA = Requirement(("gliner",), "local-extractors")
RELATION_EXTRA = Requirement(("glirel",), "local-extractors")


def build_default_registry() -> ProviderRegistry:
    """Registry with every provider this package ships."""
    registry = ProviderRegistry()
    registry.register_chat("fake", lambda s, llm: FakeChatModel(llm, s.fake))
    registry.register_chat("openai_compatible", lambda _s, llm: EndpointChatModel(llm))
    registry.register_embedding("fake", lambda s: FakeEmbeddingModel(s.embeddings))
    registry.register_embedding("openai_compatible", lambda s: EndpointEmbeddingModel(s.embeddings))
    registry.register_embedding(
        "fastembed",
        lambda s: LocalEmbeddingModel(s.embeddings, _fastembed_backend(s.embeddings)),
        EMBEDDINGS_EXTRA,
    )
    registry.register_span_extractor("fake", lambda s: FakeSpanExtractor(s.fake.dictionary))
    registry.register_span_extractor(
        "gliner",
        lambda s: LocalSpanExtractor(
            s.local_extractors.gliner, _gliner_backend(s.local_extractors.gliner)
        ),
        LOCAL_EXTRACTORS_EXTRA,
    )
    registry.register_relation_extractor(
        "glirel",
        lambda s: LocalRelationExtractor(
            s.local_extractors.glirel, _glirel_backend(s.local_extractors.glirel)
        ),
        RELATION_EXTRA,
    )
    for name in ("cooccurrence", "fake"):
        registry.register_relation_extractor(
            name, lambda s: CooccurrenceRelationExtractor(s.local_extractors.cooccurrence)
        )
    return registry


default_registry = build_default_registry()
create_chat_model = default_registry.create_chat_model
create_embedding_model = default_registry.create_embedding_model
create_span_extractor = default_registry.create_span_extractor
create_relation_extractor = default_registry.create_relation_extractor
validate_providers = default_registry.validate
