from __future__ import annotations

import pytest

from lorebase_inference import DimensionMismatchError
from lorebase_inference.local_embeddings import LocalEmbeddingModel
from lorebase_inference.settings import EmbeddingSettings


def local_settings(**overrides) -> EmbeddingSettings:
    return EmbeddingSettings(provider="fastembed", model="m", dims=2, **overrides)


async def test_local_embeddings_load_the_backend_lazily_and_once() -> None:
    built = []
    seen: list[list[str]] = []

    def factory():
        built.append(1)

        def backend(texts):
            seen.append(list(texts))
            return [[float(len(t)), 1.0] for t in texts]

        return backend

    model = LocalEmbeddingModel(local_settings(batch_size=2), factory)
    assert built == []

    assert await model.embed(["a", "bb", "ccc"]) == [[1, 1], [2, 1], [3, 1]]
    await model.embed(["d"])

    assert built == [1]
    assert seen == [["a", "bb"], ["ccc"], ["d"]]
    assert await model.embed([]) == []


async def test_local_embeddings_check_the_dimension() -> None:
    model = LocalEmbeddingModel(local_settings(), lambda: lambda texts: [[1.0, 2.0, 3.0]])
    with pytest.raises(DimensionMismatchError, match=r"3 dimensions, embeddings\.dims is 2"):
        await model.embed(["a"])


async def test_local_embeddings_check_the_count() -> None:
    model = LocalEmbeddingModel(local_settings(), lambda: lambda texts: [[1.0, 2.0]])
    with pytest.raises(DimensionMismatchError, match="1 vectors for 2 texts"):
        await model.embed(["a", "b"])
