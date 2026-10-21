"""In-process CPU embeddings: lazy model load, batching, dimension check.

The model library is reached through a backend callable supplied by the registry, so this
module imports nothing heavy. The backend is built on first use (loading or downloading the
model takes seconds), in a worker thread so that the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterable, Sequence

from lorebase_inference.errors import DimensionMismatchError
from lorebase_inference.settings import EmbeddingSettings

EmbedBackend = Callable[[Sequence[str]], Iterable[Sequence[float]]]
"""Embeds one batch of texts; yields one vector per text."""


class LocalEmbeddingModel:
    """`EmbeddingModel` running a local model through a backend."""

    def __init__(
        self, embeddings: EmbeddingSettings, backend_factory: Callable[[], EmbedBackend]
    ) -> None:
        self.name = embeddings.model
        self.dims = embeddings.dims
        self._batch_size = embeddings.batch_size
        self._factory = backend_factory
        self._backend: EmbedBackend | None = None
        self._lock = threading.Lock()

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector of `dims` floats per text, in order."""
        if not texts:
            return []
        return await asyncio.to_thread(self._embed, list(texts))

    def _embed(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            if self._backend is None:
                self._backend = self._factory()
            vectors: list[list[float]] = []
            for start in range(0, len(texts), self._batch_size):
                batch = texts[start : start + self._batch_size]
                vectors.extend([float(v) for v in vector] for vector in self._backend(batch))
        if len(vectors) != len(texts):
            raise DimensionMismatchError(
                f"model {self.name!r} returned {len(vectors)} vectors for {len(texts)} texts"
            )
        for vector in vectors:
            if len(vector) != self.dims:
                raise DimensionMismatchError(
                    f"model {self.name!r} returned {len(vector)} dimensions, "
                    f"embeddings.dims is {self.dims}"
                )
        return vectors
