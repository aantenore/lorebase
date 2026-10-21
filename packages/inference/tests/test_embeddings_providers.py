from __future__ import annotations

import httpx
import pytest

from lorebase_inference import (
    DimensionMismatchError,
    EmbeddingModel,
    EndpointError,
    InferenceSettings,
    create_embedding_model,
)
from lorebase_inference.remote_embeddings import EndpointEmbeddingModel
from lorebase_inference.settings import EmbeddingSettings, RetrySettings


def embeddings_answer(vectors: list[list[float]], *, tokens: int | None = 4) -> httpx.Response:
    body: dict = {
        "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)]
    }
    if tokens is not None:
        body["usage"] = {"prompt_tokens": tokens, "total_tokens": tokens}
    return httpx.Response(200, json=body)


def remote(endpoint, **overrides) -> EndpointEmbeddingModel:
    settings = EmbeddingSettings(
        provider="openai_compatible",
        base_url="http://emb.test/v1",
        model="embed-small",
        dims=3,
        retry=RetrySettings(max_retries=1, backoff_base_s=0.1),
        **overrides,
    )
    return EndpointEmbeddingModel(settings, transport=endpoint.transport, sleep=endpoint.sleep)


async def test_remote_embeddings_post_to_the_embeddings_route(scripted) -> None:
    endpoint = scripted(embeddings_answer([[1, 0, 0], [0, 1, 0]], tokens=6))
    model = remote(endpoint, api_key="sk-emb")

    vectors = await model.embed(["alpha", "beta"])

    assert isinstance(model, EmbeddingModel)
    assert (model.name, model.dims) == ("embed-small", 3)
    assert vectors == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    assert str(endpoint.requests[0].url) == "http://emb.test/v1/embeddings"
    assert endpoint.bodies() == [{"model": "embed-small", "input": ["alpha", "beta"]}]
    assert endpoint.requests[0].headers["authorization"] == "Bearer sk-emb"
    assert model.usage.input_tokens == 6


async def test_remote_embeddings_split_batches_and_keep_order(scripted) -> None:
    endpoint = scripted(
        embeddings_answer([[1, 0, 0], [0, 1, 0]]), embeddings_answer([[0, 0, 1]], tokens=None)
    )
    model = remote(endpoint, batch_size=2)

    vectors = await model.embed(["a", "b", "c"])

    assert [b["input"] for b in endpoint.bodies()] == [["a", "b"], ["c"]]
    assert vectors == [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    assert model.usage.input_tokens == 4 + 1, "estimated when the server reports no usage"


async def test_remote_embeddings_sort_by_index(scripted) -> None:
    answer = httpx.Response(
        200,
        json={
            "data": [
                {"index": 1, "embedding": [0, 1, 0]},
                {"index": 0, "embedding": [1, 0, 0]},
            ]
        },
    )
    assert await remote(scripted(answer)).embed(["a", "b"]) == [[1, 0, 0], [0, 1, 0]]


async def test_remote_embeddings_reject_a_wrong_dimension(scripted) -> None:
    endpoint = scripted(embeddings_answer([[1, 0]]))
    with pytest.raises(DimensionMismatchError, match=r"2 dimensions, embeddings\.dims is 3"):
        await remote(endpoint).embed(["a"])


async def test_remote_embeddings_reject_a_wrong_count(scripted) -> None:
    endpoint = scripted(embeddings_answer([[1, 0, 0]]))
    with pytest.raises(EndpointError, match="expected 2 vectors, got 1"):
        await remote(endpoint).embed(["a", "b"])


async def test_remote_embeddings_retry_on_429(scripted) -> None:
    endpoint = scripted(
        scripted.api_error(429, "slow", **{"Retry-After": "0"}), embeddings_answer([[1, 0, 0]])
    )
    assert await remote(endpoint).embed(["a"]) == [[1, 0, 0]]
    assert len(endpoint.requests) == 2


async def test_remote_embeddings_of_nothing_make_no_request(scripted) -> None:
    endpoint = scripted(embeddings_answer([]))
    assert await remote(endpoint).embed([]) == []
    assert endpoint.requests == []


def test_registry_builds_the_remote_provider_from_configuration() -> None:
    settings = InferenceSettings.model_validate(
        {"embeddings": {"provider": "openai_compatible", "base_url": "http://e/v1"}}
    )
    assert isinstance(create_embedding_model(settings), EndpointEmbeddingModel)


@pytest.mark.parametrize(("configured", "expected"), [({}, False), ({"trust_env": True}, True)])
async def test_remote_embeddings_trust_env_follows_the_settings(
    scripted, client_options, configured, expected
) -> None:
    endpoint = scripted(embeddings_answer([[1, 0, 0]]))
    model = remote(endpoint, **configured)
    await model.embed(["alpha"])
    await model.aclose()
    assert [options["trust_env"] for options in client_options] == [expected]
