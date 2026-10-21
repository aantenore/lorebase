"""Model-agnostic inference: interfaces, provider registry and a deterministic fake provider.

Importing the package registers the `inference` settings section (with the `LLM_*` environment
aliases) with `lorebase_platform`.
"""

from lorebase_inference.errors import (
    DimensionMismatchError,
    EndpointError,
    InferenceError,
    InvalidOutputError,
    ProviderUnavailableError,
)
from lorebase_inference.interfaces import (
    ChatModel,
    ChatOptions,
    ChatResult,
    EmbeddingModel,
    JsonSchema,
    Label,
    Message,
    Span,
    SpanExtractor,
    Usage,
)
from lorebase_inference.registry import (
    ProviderRegistry,
    UnknownProviderError,
    create_chat_model,
    create_embedding_model,
    create_span_extractor,
    validate_providers,
)
from lorebase_inference.settings import InferenceSettings, inference_settings

__version__ = "0.1.0"

__all__ = [
    "ChatModel",
    "ChatOptions",
    "ChatResult",
    "DimensionMismatchError",
    "EmbeddingModel",
    "EndpointError",
    "InferenceError",
    "InferenceSettings",
    "InvalidOutputError",
    "JsonSchema",
    "Label",
    "Message",
    "ProviderRegistry",
    "ProviderUnavailableError",
    "Span",
    "SpanExtractor",
    "UnknownProviderError",
    "Usage",
    "create_chat_model",
    "create_embedding_model",
    "create_span_extractor",
    "inference_settings",
    "validate_providers",
]
