"""Text embeddings with the configured model, prefixed as Scribe prefixes documents.

`document` mode puts `inference.embeddings.document_prefix` before the text, exactly as Scribe
does before storing a Record's vector, so the same text gives the same vector. `query` mode
puts `query_prefix` before it, as `lorebase-inference embed --query` does. The model name
returned is the one vectors are stored under, which Keeper advertises in `GET .../schema`
(`search.embedding`); a semantic search is only meaningful when both match.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from lorebase_inference import (
    DimensionMismatchError,
    EmbeddingModel,
    InferenceSettings,
    create_embedding_model,
)

EmbedMode = Literal["query", "document"]


@dataclass(frozen=True)
class Embedding:
    model: str
    dims: int
    vector: list[float]


class Embedder:
    """The process's embedding model, created on first use and shared by every request."""

    def __init__(
        self,
        inference: InferenceSettings,
        *,
        factory: Callable[[], EmbeddingModel] | None = None,
    ) -> None:
        self._settings = inference.embeddings
        self._factory = factory or (lambda: create_embedding_model(inference))
        self._model: EmbeddingModel | None = None
        self._lock = asyncio.Lock()

    def prefixed(self, text: str, mode: EmbedMode) -> str:
        """The text the model sees in `mode`."""
        prefix = self._settings.query_prefix if mode == "query" else self._settings.document_prefix
        return prefix + text

    async def model(self) -> EmbeddingModel:
        if self._model is None:
            async with self._lock:
                if self._model is None:
                    self._model = self._factory()
        return self._model

    async def embed(self, text: str, mode: EmbedMode) -> Embedding:
        """The vector of `text` in `mode`; its size must be `inference.embeddings.dims`."""
        model = await self.model()
        vectors = await model.embed([self.prefixed(text, mode)])
        vector = [float(x) for x in vectors[0]] if vectors else []
        if len(vector) != self._settings.dims:
            raise DimensionMismatchError(
                f"model returned {len(vector)} dimensions, embeddings.dims is {self._settings.dims}"
            )
        return Embedding(model=model.name, dims=len(vector), vector=vector)

    async def aclose(self) -> None:
        close = getattr(self._model, "aclose", None)
        if close is not None:
            await close()
