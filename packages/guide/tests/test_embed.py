from __future__ import annotations

import json

import pytest

from lorebase_scribe.embeddings import EmbeddingModels

from .support import (
    ALL_KINDS,
    DOMAIN,
    FakeKeeper,
    ScriptedChat,
    bearer,
    events,
    inference_settings,
    named,
    open_app,
)

PREFIXES = {"query_prefix": "query: ", "document_prefix": "passage: "}


async def test_document_mode_matches_scribe_document_vectors(keeper: FakeKeeper) -> None:
    inference = inference_settings(**PREFIXES)
    text = "Acme Metals, steel supplier in Turin"
    async with open_app(keeper, inference=inference) as http:
        response = await http.post(
            "/embed", json={"text": text, "mode": "document"}, headers=bearer(ALL_KINDS)
        )
    assert response.status_code == 200, response.text
    body = response.json()

    models = EmbeddingModels(inference, dims=inference.embeddings.dims)
    scribe_model = await models.get()
    [expected] = await scribe_model.embed([models.document_prefix + text])

    assert body["vector"] == pytest.approx(expected)
    assert body["model"] == scribe_model.name
    assert body["dims"] == inference.embeddings.dims == len(body["vector"])


async def test_model_and_dims_match_what_keeper_advertises(keeper: FakeKeeper) -> None:
    async with open_app(keeper) as http:
        body = (await http.post("/embed", json={"text": "x"}, headers=bearer(ALL_KINDS))).json()
    advertised = keeper._schema()["search"]["embedding"]
    assert (body["model"], body["dims"]) == (advertised["model"], advertised["dims"])


async def test_query_mode_applies_the_query_prefix(keeper: FakeKeeper) -> None:
    inference = inference_settings(**PREFIXES)
    async with open_app(keeper, inference=inference) as http:
        query = await http.post(
            "/embed", json={"text": "steel", "mode": "query"}, headers=bearer(ALL_KINDS)
        )
        document = await http.post(
            "/embed", json={"text": "steel", "mode": "document"}, headers=bearer(ALL_KINDS)
        )
        prefixed = await http.post(
            "/embed", json={"text": "query: steel", "mode": "document"}, headers=bearer(ALL_KINDS)
        )
    assert query.json()["vector"] != document.json()["vector"]
    no_document_prefix = inference_settings(query_prefix="query: ")
    async with open_app(keeper, inference=no_document_prefix) as http:
        plain = await http.post(
            "/embed", json={"text": "query: steel", "mode": "document"}, headers=bearer(ALL_KINDS)
        )
    assert query.json()["vector"] == plain.json()["vector"]
    assert prefixed.json()["vector"] != query.json()["vector"]


async def test_embed_needs_a_credential_keeper_accepts(keeper: FakeKeeper) -> None:
    async with open_app(keeper) as http:
        anonymous = await http.post("/embed", json={"text": "x"})
        unknown = await http.post("/embed", json={"text": "x"}, headers=bearer("lbk_nope"))
    assert anonymous.status_code == 401
    assert unknown.status_code == 401


async def test_embed_limits(keeper: FakeKeeper) -> None:
    async with open_app(keeper, limits={"embeds_per_minute": 1, "max_embed_chars": 16}) as http:
        too_long = await http.post("/embed", json={"text": "x" * 17}, headers=bearer(ALL_KINDS))
        first = await http.post("/embed", json={"text": "x"}, headers=bearer(ALL_KINDS))
        second = await http.post("/embed", json={"text": "x"}, headers=bearer(ALL_KINDS))
    assert too_long.status_code == 422
    assert first.status_code == 200
    assert second.status_code == 429


@pytest.mark.parametrize(
    ("embedding_model", "expected_vector"), [("embed", True), ("other", False)]
)
async def test_semantic_search_uses_the_query_vector_only_when_models_match(
    keeper: FakeKeeper, embedding_model: str, expected_vector: bool
) -> None:
    chat = ScriptedChat(
        [
            {"thought": "s", "action": "search", "args": {"q": "acme", "mode": "hybrid"}},
            {"thought": "a", "action": "answer", "answer": "x", "citations": [], "found": False},
        ]
    )
    inference = inference_settings(model=embedding_model)
    async with open_app(keeper, chat, inference=inference) as http:
        response = await http.post(
            "/explore", json={"domain": DOMAIN, "question": "acme?"}, headers=bearer(ALL_KINDS)
        )
    search = next(r for r in keeper.requests if r.url.path.endswith("/search"))
    sent = json.loads(search.content)
    assert (sent.get("query_vector") is not None) is expected_vector
    assert sent["mode"] == ("hybrid" if expected_vector else "lexical")
    assert named(events(response.text), "tool_result")[0]["ok"] is True
