"""Embeddings from a compatible HTTP endpoint: `POST {base_url}/embeddings`.

Works with any local server, proxy or hosted endpoint that implements the embeddings route
(docs/inference-providers.md lists the tested ones). Texts are sent in batches of `batch_size`;
the answer is checked for count and dimension so that a model swap cannot silently corrupt a
vector index.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import httpx

from lorebase_inference.errors import DimensionMismatchError, EndpointError
from lorebase_inference.interfaces import Usage
from lorebase_inference.settings import EmbeddingSettings
from lorebase_inference.transport import EndpointClient, Sleep

_CHARS_PER_TOKEN = 4


class EndpointEmbeddingModel:
    """`EmbeddingModel` for an HTTP embeddings endpoint."""

    def __init__(
        self,
        embeddings: EmbeddingSettings,
        *,
        max_concurrency: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        if not embeddings.base_url:
            raise ValueError("embeddings.base_url is required for this provider")
        self.name = embeddings.model
        self.dims = embeddings.dims
        self.usage = Usage()
        """Tokens sent so far, summed over calls (estimated when the server omits counts)."""
        self._batch_size = embeddings.batch_size
        self._client = EndpointClient(
            base_url=embeddings.base_url,
            api_key=embeddings.api_key,
            timeout_s=embeddings.timeout_s,
            retry=embeddings.retry,
            trust_env=embeddings.trust_env,
            max_concurrency=max_concurrency,
            transport=transport,
            sleep=sleep,
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector of `dims` floats per text, in order."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            vectors.extend(await self._embed_batch(list(texts[start : start + self._batch_size])))
        return vectors

    async def aclose(self) -> None:
        """Release connections."""
        await self._client.aclose()

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        body = await self._client.post("/embeddings", {"model": self.name, "input": batch})
        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(batch):
            raise EndpointError(
                f"/embeddings: expected {len(batch)} vectors, got "
                f"{len(data) if isinstance(data, list) else 'none'}"
            )
        ordered = sorted(
            data, key=lambda item: item.get("index", 0) if isinstance(item, dict) else 0
        )
        vectors = [_vector(item) for item in ordered]
        for vector in vectors:
            if len(vector) != self.dims:
                raise DimensionMismatchError(
                    f"model {self.name!r} returned {len(vector)} dimensions, "
                    f"embeddings.dims is {self.dims}"
                )
        raw = body.get("usage")
        reported = raw.get("prompt_tokens") if isinstance(raw, dict) else None
        tokens = (
            reported
            if isinstance(reported, int) and not isinstance(reported, bool)
            else math.ceil(sum(len(t) for t in batch) / _CHARS_PER_TOKEN)
        )
        self.usage = Usage(self.usage.input_tokens + tokens, 0)
        return vectors


def _vector(item: Any) -> list[float]:
    embedding = item.get("embedding") if isinstance(item, dict) else None
    if not isinstance(embedding, list) or not all(
        isinstance(v, int | float) and not isinstance(v, bool) for v in embedding
    ):
        raise EndpointError("/embeddings: entry without a numeric vector")
    return [float(v) for v in embedding]
